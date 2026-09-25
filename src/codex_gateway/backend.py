from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

from .app_server import AppServerError, AppServerPool
from .config import Settings
from .schemas import BackendResult, BackendStreamEvent, ResponseRequest


@dataclass(frozen=True)
class BackendTarget:
    connection_key: str
    endpoint: str
    workspace: str
    worker_id: UUID | None = None


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


def _token_counts(params: dict[str, Any]) -> tuple[int, int]:
    usage = params.get("tokenUsage") or params.get("usage") or params
    if isinstance(usage, dict) and isinstance(usage.get("total"), dict):
        usage = usage["total"]
    if not isinstance(usage, dict):
        return 0, 0
    return int(usage.get("inputTokens", usage.get("input_tokens", 0)) or 0), int(usage.get("outputTokens", usage.get("output_tokens", 0)) or 0)


class AppServerBackend:
    def __init__(self, settings: Settings) -> None:
        self.token = settings.app_server_token.get_secret_value()
        self.timeout = settings.app_server_timeout_seconds
        self.public_model = settings.model_name
        self.upstream_model = settings.upstream_model
        self.pool = AppServerPool(self.token, self.timeout)

    def model(self, requested: str) -> str:
        return self.upstream_model if requested == self.public_model else requested

    async def _start_thread(self, app_server, request: ResponseRequest, workspace: str) -> str:
        if request.previous_response_id:
            result = await app_server.call("thread/resume", {"threadId": request.previous_response_id})
        else:
            result = await app_server.call("thread/start", {"model": self.model(request.model), "cwd": workspace, "approvalPolicy": "never", "sandbox": "workspace-write", "serviceName": "codex_gateway"})
        return result["thread"]["id"]

    async def _turn(self, request: ResponseRequest, target: BackendTarget) -> AsyncIterator[BackendStreamEvent]:
        async with self.pool.lock(target.connection_key):
            try:
                app_server = await self.pool.session(target.connection_key, target.endpoint)
                account = await app_server.call("account/read", {"refreshToken": False})
                if account.get("requiresOpenaiAuth") and not account.get("account"):
                    raise AppServerError("Codex worker is not logged in")
                thread_id = await self._start_thread(app_server, request, target.workspace)
                turn_params = {
                    "threadId": thread_id, "input": [{"type": "text", "text": request.input_text()}],
                    "cwd": target.workspace, "approvalPolicy": "never",
                    "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [target.workspace], "networkAccess": False},
                    "model": self.model(request.model),
                }
                effort = (request.reasoning or {}).get("effort")
                if effort:
                    turn_params["effort"] = effort
                if output_schema := request.output_schema():
                    turn_params["outputSchema"] = output_schema
                await app_server.call("turn/start", turn_params)
                input_tokens = output_tokens = 0
                async for message in app_server.messages():
                    method, params = message.get("method"), message.get("params", {})
                    if message.get("id") is not None and method:
                        await app_server.reject_server_request(message)
                    elif method == "item/agentMessage/delta":
                        yield BackendStreamEvent(delta=params.get("delta", ""), thread_id=thread_id)
                    elif method == "thread/tokenUsage/updated":
                        input_tokens, output_tokens = _token_counts(params)
                    elif method == "turn/completed":
                        turn = params.get("turn", {})
                        if turn.get("status") == "failed":
                            error = turn.get("error") or {}
                            raise AppServerError(error.get("message", "Codex turn failed"))
                        yield BackendStreamEvent(thread_id=thread_id, done=True, input_tokens=input_tokens, output_tokens=output_tokens)
                        return
            except AppServerError:
                raise
            except Exception as exc:
                await self.pool.invalidate(target.connection_key)
                raise AppServerError(f"Codex worker connection failed: {exc}") from exc

    async def complete(self, request: ResponseRequest, target: BackendTarget) -> BackendResult:
        chunks: list[str] = []
        terminal = BackendStreamEvent()
        async for event in self._turn(request, target):
            terminal = event
            if event.delta:
                chunks.append(event.delta)
        return BackendResult(text="".join(chunks), thread_id=terminal.thread_id or "", input_tokens=terminal.input_tokens, output_tokens=terminal.output_tokens)

    async def stream(self, request: ResponseRequest, target: BackendTarget) -> AsyncIterator[BackendStreamEvent]:
        async for event in self._turn(request, target):
            yield event

    async def close(self) -> None:
        await self.pool.close()
