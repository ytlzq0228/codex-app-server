import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

from .app_server import AppServerCapacityError, AppServerError, AppServerPool
from .config import Settings
from .schemas import BackendResult, BackendStreamEvent, ResponseRequest


@dataclass(frozen=True)
class BackendTarget:
    connection_key: str
    endpoint: str
    workspace: str
    worker_id: UUID | None = None


class WorkerFailure(RuntimeError):
    """A worker-scoped failure with enough state for safe failover decisions."""

    def __init__(self, message: str, *, kind: str = "connection", safe_to_retry: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.safe_to_retry = safe_to_retry


def classify_worker_failure(message: str) -> str:
    lowered = message.lower().replace("_", " ").replace("-", " ")
    if any(term in lowered for term in ("invalid request", "model is not supported", "model is unsupported", "unsupported model")):
        return "request"
    if any(term in lowered for term in ("not logged in", "login", "auth", "unauthorized", "401")):
        return "logged_out"
    if any(term in lowered for term in ("usage limit", "rate limit", "limit reached", "quota", "too many requests", "429", "5 hour", "five hour")):
        return "limit"
    return "connection"


async def run_healthcheck_turn(app_server: Any, model: str, cwd: str = "/workspace") -> None:
    """Run a minimal real turn; account/read alone cannot detect exhausted quota."""
    result = await app_server.call(
        "thread/start",
        {"model": model, "cwd": cwd, "approvalPolicy": "never", "sandbox": "workspace-write", "serviceName": "codex_gateway_healthcheck"},
    )
    thread_id = result["thread"]["id"]
    await app_server.call(
        "turn/start",
        {
            "threadId": thread_id,
            "input": [{"type": "text", "text": "Reply with OK only."}],
            "cwd": cwd,
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [cwd], "networkAccess": False},
            "model": model,
            "effort": "low",
        },
    )
    async for message in app_server.messages():
        method, params = message.get("method"), message.get("params", {})
        if message.get("id") is not None and method:
            await app_server.reject_server_request(message)
        elif method == "turn/completed":
            turn = params.get("turn", {})
            if turn.get("status") == "failed":
                error = turn.get("error") or {}
                detail = error.get("message", error) if isinstance(error, dict) else error
                detail = str(detail or "Codex health-check turn failed")
                raise WorkerFailure(detail, kind=classify_worker_failure(detail), safe_to_retry=False)
            return


class CompletionBackend(Protocol):
    async def complete(self, request: ResponseRequest, target: BackendTarget) -> BackendResult: ...
    async def stream(self, request: ResponseRequest, target: BackendTarget) -> AsyncIterator[BackendStreamEvent]: ...
    async def close(self) -> None: ...


class MockBackend:
    async def complete(self, request: ResponseRequest, target: BackendTarget) -> BackendResult:
        source = request.input_text()
        text = f"mock: {source}"
        return BackendResult(text=text, thread_id=request.previous_response_id or f"thr_mock_{uuid4().hex}", input_tokens=len(source.split()), output_tokens=len(text.split()))

    async def stream(self, request: ResponseRequest, target: BackendTarget) -> AsyncIterator[BackendStreamEvent]:
        result = await self.complete(request, target)
        for token in result.text.split(" "):
            yield BackendStreamEvent(delta=token + " ", thread_id=result.thread_id)
        yield BackendStreamEvent(thread_id=result.thread_id, done=True, input_tokens=result.input_tokens, output_tokens=result.output_tokens)

    async def close(self) -> None:
        return None


def _token_counts(params: dict[str, Any]) -> tuple[int, int, int, int]:
    usage = params.get("tokenUsage") or params.get("usage") or params
    if isinstance(usage, dict):
        usage = usage.get("last") or usage.get("total") or usage
    if not isinstance(usage, dict):
        return 0, 0, 0, 0
    return tuple(int(usage.get(name, usage.get(alias, 0)) or 0) for name, alias in (
        ("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
        ("cachedInputTokens", "cache_read_tokens"), ("cacheWriteInputTokens", "cache_write_tokens")))


class TurnUsage:
    """Accumulate model calls in one turn without billing thread totals repeatedly."""

    def __init__(self):
        self.counts = (0, 0, 0, 0)
        self.previous_total = None

    def observe(self, params):
        usage = params.get("tokenUsage") or params.get("usage") or params
        total = usage.get("total") if isinstance(usage, dict) else None
        if self.previous_total is not None and isinstance(total, dict):
            current = _token_counts(total)
            increment = (_token_counts(params) if any(new < old for new, old in zip(current, self.previous_total))
                         else tuple(new - old for new, old in zip(current, self.previous_total)))
        else:
            increment = _token_counts(params)
        self.counts = tuple(old + new for old, new in zip(self.counts, increment))
        if isinstance(total, dict):
            self.previous_total = _token_counts(total)
        return self.counts


class AppServerBackend:
    def __init__(self, settings: Settings) -> None:
        self.token = settings.app_server_token.get_secret_value()
        self.timeout = settings.app_server_timeout_seconds
        self.model_aliases = settings.model_alias_map()
        self.pool = AppServerPool(
            self.token, self.timeout,
            max_per_group=settings.max_ws_per_key_worker,
            max_per_worker=settings.max_ws_per_worker,
            idle_ttl=settings.ws_idle_ttl_seconds,
            acquire_timeout=settings.ws_acquire_timeout_seconds,
        )
        self._thread_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._thread_locks_guard = asyncio.Lock()

    @asynccontextmanager
    async def _thread_guard(self, thread_id: str | None):
        if not thread_id:
            yield
            return
        async with self._thread_locks_guard:
            lock, users = self._thread_locks.get(thread_id, (asyncio.Lock(), 0))
            self._thread_locks[thread_id] = (lock, users + 1)
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            async with self._thread_locks_guard:
                current_lock, users = self._thread_locks[thread_id]
                if users <= 1:
                    self._thread_locks.pop(thread_id, None)
                else:
                    self._thread_locks[thread_id] = (current_lock, users - 1)

    def model(self, requested: str) -> str:
        return self.model_aliases.get(requested, requested)

    async def _start_thread(self, app_server, request: ResponseRequest, workspace: str) -> str:
        if request.previous_response_id:
            try:
                result = await app_server.call("thread/resume", {"threadId": request.previous_response_id})
            except AppServerError as exc:
                raise WorkerFailure("The previous response session can no longer be resumed", kind="session", safe_to_retry=False) from exc
        else:
            result = await app_server.call("thread/start", {"model": self.model(request.model), "cwd": workspace, "approvalPolicy": "never", "sandbox": "workspace-write", "serviceName": "codex_gateway"})
        return result["thread"]["id"]

    async def _turn(self, request: ResponseRequest, target: BackendTarget) -> AsyncIterator[BackendStreamEvent]:
        worker_key = str(target.worker_id or target.endpoint)
        slot_id: int | None = None
        async with self._thread_guard(request.previous_response_id):
            turn_may_have_started = False
            try:
                async with self.pool.lease(target.connection_key, worker_key, target.endpoint) as (app_server, slot_id):
                    workspace = f"{target.workspace}/ws-{slot_id}"
                    account = await app_server.call("account/read", {"refreshToken": False})
                    if account.get("requiresOpenaiAuth") and not account.get("account"):
                        raise WorkerFailure("Codex worker is not logged in", kind="logged_out", safe_to_retry=True)
                    thread_id = await self._start_thread(app_server, request, workspace)
                    turn_params = {
                        "threadId": thread_id, "input": [{"type": "text", "text": request.input_text()}],
                        "cwd": workspace, "approvalPolicy": "never",
                        "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [workspace], "networkAccess": False},
                        "model": self.model(request.model),
                    }
                    effort = (request.reasoning or {}).get("effort")
                    if effort:
                        turn_params["effort"] = effort
                    if output_schema := request.output_schema():
                        turn_params["outputSchema"] = output_schema
                    turn_may_have_started = True
                    started = await app_server.call("turn/start", turn_params)
                    turn_id = (started.get("turn") or {}).get("id")
                    turn_usage = TurnUsage()
                    async for message in app_server.messages():
                        method, params = message.get("method"), message.get("params", {})
                        if message.get("id") is not None and method:
                            await app_server.reject_server_request(message)
                        elif method == "item/agentMessage/delta":
                            yield BackendStreamEvent(delta=params.get("delta", ""), thread_id=thread_id)
                        elif method == "thread/tokenUsage/updated":
                            if not turn_id or not params.get("turnId") or params["turnId"] == turn_id:
                                turn_usage.observe(params)
                        elif method == "turn/completed":
                            turn = params.get("turn", {})
                            if turn.get("status") == "failed":
                                error = turn.get("error") or {}
                                message = error.get("message", "Codex turn failed")
                                raise WorkerFailure(message, kind=classify_worker_failure(message), safe_to_retry=False)
                            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens = turn_usage.counts
                            yield BackendStreamEvent(thread_id=thread_id, done=True, input_tokens=input_tokens,
                                                     output_tokens=output_tokens, cache_read_tokens=cache_read_tokens,
                                                     cache_write_tokens=cache_write_tokens)
                            return
            except WorkerFailure:
                raise
            except AppServerCapacityError as exc:
                raise WorkerFailure(str(exc), kind="capacity", safe_to_retry=False) from exc
            except AppServerError as exc:
                raise WorkerFailure(str(exc), kind=classify_worker_failure(str(exc)), safe_to_retry=not turn_may_have_started) from exc
            except Exception as exc:
                if slot_id is not None:
                    await self.pool.invalidate(target.connection_key, slot_id)
                raise WorkerFailure(f"Codex worker connection failed: {exc}", kind="connection", safe_to_retry=not turn_may_have_started) from exc

    async def complete(self, request: ResponseRequest, target: BackendTarget) -> BackendResult:
        chunks: list[str] = []
        terminal = BackendStreamEvent()
        async for event in self._turn(request, target):
            terminal = event
            if event.delta:
                chunks.append(event.delta)
        return BackendResult(text="".join(chunks), thread_id=terminal.thread_id or "", input_tokens=terminal.input_tokens,
                             output_tokens=terminal.output_tokens, cache_read_tokens=terminal.cache_read_tokens,
                             cache_write_tokens=terminal.cache_write_tokens)

    async def stream(self, request: ResponseRequest, target: BackendTarget) -> AsyncIterator[BackendStreamEvent]:
        async for event in self._turn(request, target):
            yield event

    async def close(self) -> None:
        await self.pool.close()
