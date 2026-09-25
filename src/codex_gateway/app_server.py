import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from websockets.asyncio.client import connect


class AppServerError(RuntimeError):
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
        await self.websocket.send(json.dumps({"method": method, "id": request_id, "params": params or {}}))
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


class AppServerPool:
    """Persistent app-server sessions keyed by API key and worker."""

    def __init__(self, token: str, timeout: float) -> None:
        self.token = token
        self.timeout = timeout
        self._sessions: dict[str, AppServerSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._pool_lock = asyncio.Lock()

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def session(self, key: str, url: str) -> AppServerSession:
        async with self._pool_lock:
            current = self._sessions.get(key)
            if current and not current.closed:
                return current
            if current:
                await current.close()
            current = await connect_app_server(url, self.token, self.timeout)
            self._sessions[key] = current
            return current

    async def invalidate(self, key: str) -> None:
        async with self._pool_lock:
            current = self._sessions.pop(key, None)
        if current:
            await current.close()

    async def close(self) -> None:
        async with self._pool_lock:
            sessions, self._sessions = list(self._sessions.values()), {}
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
