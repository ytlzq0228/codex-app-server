import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect


class AppServerError(RuntimeError):
    pass


class AppServerCapacityError(RuntimeError):
    pass


class AppServerSession:
    """One initialized app-server connection, serialized by the backend per turn."""

    def __init__(self, websocket: Any, timeout: float) -> None:
        self.websocket = websocket
        self.timeout = timeout
        self.next_id = 1
        self.pending_notifications: list[dict[str, Any]] = []

    @property
    def closed(self) -> bool:
        return self.websocket.state.name == "CLOSED"

    async def send(self, method: str, params: dict[str, Any] | None = None) -> int:
        request_id = self.next_id
        self.next_id += 1
        # The account/logout RPC has a null parameter schema, unlike object RPCs.
        rpc_params = None if method == "account/logout" else params or {}
        await self.websocket.send(json.dumps({"method": method, "id": request_id, "params": rpc_params}))
        return request_id

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.websocket.send(json.dumps({"method": method, "params": params or {}}))

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        while self.pending_notifications:
            yield self.pending_notifications.pop(0)
        while True:
            raw = await asyncio.wait_for(self.websocket.recv(), self.timeout)
            yield json.loads(raw)

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = await self.send(method, params)
        while True:
            raw = await asyncio.wait_for(self.websocket.recv(), self.timeout)
            message = json.loads(raw)
            if message.get("id") != request_id:
                if message.get("id") is not None and message.get("method"):
                    await self.reject_server_request(message)
                elif message.get("method"):
                    self.pending_notifications.append(message)
                continue
            if "error" in message:
                error = message["error"]
                raise AppServerError(f"{method}: {error.get('message', error)}")
            return message.get("result", {})

    async def reject_server_request(self, message: dict[str, Any]) -> None:
        method = message.get("method", "")
        result = {"decision": "decline"} if "requestApproval" in method else {"error": "unsupported client request"}
        await self.websocket.send(json.dumps({"id": message["id"], "result": result}))

    async def close(self) -> None:
        await self.websocket.close()


async def connect_app_server(url: str, token: str, timeout: float = 300.0) -> AppServerSession:
    websocket = await connect(url, additional_headers={"Authorization": f"Bearer {token}"}, open_timeout=15)
    session = AppServerSession(websocket, timeout)
    try:
        await session.call("initialize", {"clientInfo": {"name": "codex_gateway", "title": "Codex Gateway", "version": "0.3.0"}})
        await session.notify("initialized")
    except Exception:
        await session.close()
        raise
    return session


@asynccontextmanager
async def open_app_server(url: str, token: str, timeout: float = 300.0) -> AsyncIterator[AppServerSession]:
    session = await connect_app_server(url, token, timeout)
    try:
        yield session
    finally:
        await session.close()


@dataclass
class AppServerSlot:
    group_key: str
    worker_key: str
    slot_id: int
    session: AppServerSession | None = None
    busy: bool = True
    last_used: float = 0.0


class AppServerPool:
    """Bounded persistent sessions with per-Key and per-Worker capacity limits."""

    def __init__(self, token: str, timeout: float, max_per_group: int = 10, max_per_worker: int = 40, idle_ttl: float = 600.0, acquire_timeout: float = 30.0) -> None:
        self.token = token
        self.timeout = timeout
        self.max_per_group = max_per_group
        self.max_per_worker = max_per_worker
        self.idle_ttl = idle_ttl
        self.acquire_timeout = acquire_timeout
        self._slots: list[AppServerSlot] = []
        self._condition = asyncio.Condition()
        self._closed = False
        self._reaper: asyncio.Task | None = None

    def _ensure_reaper(self) -> None:
        if not self._reaper:
            self._reaper = asyncio.create_task(self._reap_loop(), name="app-server-pool-reaper")

    async def _drop_stale_locked(self) -> None:
        now = time.monotonic()
        stale = [slot for slot in self._slots if not slot.busy and (not slot.session or slot.session.closed or now - slot.last_used >= self.idle_ttl)]
        for slot in stale:
            self._slots.remove(slot)
        if stale:
            await asyncio.gather(*(slot.session.close() for slot in stale if slot.session), return_exceptions=True)
            self._condition.notify_all()

    async def _reap_loop(self) -> None:
        interval = max(1.0, min(60.0, self.idle_ttl / 2))
        try:
            while True:
                await asyncio.sleep(interval)
                async with self._condition:
                    await self._drop_stale_locked()
        except asyncio.CancelledError:
            raise

    @asynccontextmanager
    async def lease(self, group_key: str, worker_key: str, url: str) -> AsyncIterator[tuple[AppServerSession, int]]:
        self._ensure_reaper()
        deadline = time.monotonic() + self.acquire_timeout
        slot: AppServerSlot | None = None
        needs_connect = False
        while slot is None:
            async with self._condition:
                if self._closed:
                    raise RuntimeError("app-server pool is closed")
                await self._drop_stale_locked()
                slot = next((item for item in self._slots if item.group_key == group_key and not item.busy), None)
                if slot:
                    slot.busy = True
                    break
                group_slots = [item for item in self._slots if item.group_key == group_key]
                worker_slots = [item for item in self._slots if item.worker_key == worker_key]
                if len(group_slots) < self.max_per_group and len(worker_slots) < self.max_per_worker:
                    used_ids = {item.slot_id for item in group_slots}
                    slot_id = next(index for index in range(self.max_per_group) if index not in used_ids)
                    slot = AppServerSlot(group_key=group_key, worker_key=worker_key, slot_id=slot_id)
                    self._slots.append(slot)
                    needs_connect = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerCapacityError("Timed out waiting for an available Codex worker connection")
                try:
                    await asyncio.wait_for(self._condition.wait(), remaining)
                except TimeoutError as exc:
                    raise AppServerCapacityError("Timed out waiting for an available Codex worker connection") from exc
        try:
            if needs_connect:
                try:
                    slot.session = await connect_app_server(url, self.token, self.timeout)
                except Exception:
                    async with self._condition:
                        if slot in self._slots:
                            self._slots.remove(slot)
                        self._condition.notify_all()
                    raise
            if not slot.session:
                raise RuntimeError("app-server connection slot was not initialized")
            yield slot.session, slot.slot_id
        finally:
            async with self._condition:
                if slot in self._slots:
                    slot.busy = False
                    slot.last_used = time.monotonic()
                self._condition.notify_all()

    async def invalidate(self, group_key: str, slot_id: int) -> None:
        async with self._condition:
            slot = next((item for item in self._slots if item.group_key == group_key and item.slot_id == slot_id), None)
            if slot:
                self._slots.remove(slot)
            self._condition.notify_all()
        if slot and slot.session:
            await slot.session.close()

    async def close(self) -> None:
        self._closed = True
        if self._reaper:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
        async with self._condition:
            slots, self._slots = self._slots, []
            self._condition.notify_all()
        await asyncio.gather(*(slot.session.close() for slot in slots if slot.session), return_exceptions=True)
