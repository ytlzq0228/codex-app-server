"""Antigravity transport, kept separate from the existing Codex backend."""
import json
from datetime import datetime, timedelta, timezone
import httpx
from .backend import AppServerBackend, WorkerFailure, classify_worker_failure
from .schemas import BackendResult, BackendStreamEvent

async def worker_rpc(endpoint, settings, path, payload=None):
    async with httpx.AsyncClient(timeout=90 if path == "/login/verify" else 45) as client:
        response = await client.post(endpoint + path, json=payload or {},
            headers={"Authorization": "Bearer " + settings.app_server_token.get_secret_value()})
        response.raise_for_status()
        return response.json()

class GeminiAdapter:
    def __init__(self, settings):
        self.settings = settings

    async def stream(self, request, target):
        from .providers import validate_capabilities, provider_for
        validate_capabilities(request)
        if target.provider != provider_for(request.model):
            raise WorkerFailure("Provider mismatch", kind="request")
        if target.worker_id:
            from .database import SessionLocal
            from .models import Worker
            async with SessionLocal() as db:
                worker = await db.get(Worker, target.worker_id)
                if not worker or worker.provider != "gemini" or worker.execution_generation != target.worker_generation or worker.endpoint != target.endpoint:
                    raise WorkerFailure("Worker identity changed", kind="account_changed")
        payload = {"prompt": request.input_text(), "model": self.settings.model_alias_map().get(request.model, request.model),
                   "conversation": request.previous_response_id, "workspace": target.workspace}
        thread = ""
        # A connection failure is deliberately not automatically retried: the
        # remote CLI may already have accepted the prompt.
        try:
            async with httpx.AsyncClient(timeout=self.settings.app_server_timeout_seconds) as client:
                async with client.stream("POST", target.endpoint + "/turn", json=payload,
                        headers={"Authorization": "Bearer " + self.settings.app_server_token.get_secret_value()}) as response:
                    if response.status_code != 200:
                        raise WorkerFailure("Gemini worker rejected execution", kind="capacity" if response.status_code == 409 else "connection")
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        data = json.loads(line)
                        if data.get("error"):
                            raise WorkerFailure(data["error"], kind=data.get("kind", "connection"))
                        thread = data.get("thread_id") or thread
                        yield BackendStreamEvent(thread_id=thread, delta=data.get("delta", ""), done=data.get("done", False),
                            input_tokens=data.get("input_tokens", 0), output_tokens=data.get("output_tokens", 0),
                            cache_read_tokens=data.get("cache_read_tokens", 0))
                        if data.get("done"):
                            return
            raise WorkerFailure("Gemini stream ended without a final result")
        except httpx.HTTPError as exc:
            raise WorkerFailure("Gemini worker connection failed") from exc

    async def complete(self, request, target):
        text = ""
        async for event in self.stream(request, target):
            text += event.delta or ""
            if event.done:
                return BackendResult(text=text, thread_id=event.thread_id, input_tokens=event.input_tokens,
                                     output_tokens=event.output_tokens, cache_read_tokens=event.cache_read_tokens)
        raise WorkerFailure("Gemini returned no final result")

class ProviderBackend:
    def __init__(self, settings):
        self.codex = AppServerBackend(settings)
        self.gemini = GeminiAdapter(settings)
        # Preserve existing Codex pool monitoring and client-tool continuations.
        self.pool = self.codex.pool
        self.tool_sessions = self.codex.tool_sessions

    def continuation_target(self, request, key):
        return self.codex.continuation_target(request, key)

    def continuation_thread(self, request, key):
        return self.codex.continuation_thread(request, key)

    def adapter(self, target):
        if target.provider == "codex":
            return self.codex
        if target.provider == "gemini":
            return self.gemini
        raise WorkerFailure("Provider is not implemented", kind="request")

    async def complete(self, request, target):
        return await self.adapter(target).complete(request, target)

    async def stream(self, request, target):
        async for event in self.adapter(target).stream(request, target):
            yield event

    async def close(self):
        await self.codex.close()

async def probe_gemini(worker, db, settings, *, inference=True, login_session=None):
    from .contributions import update_account
    from .models import WorkerStatus
    from .quota import reconcile_worker
    try:
        payload = await worker_rpc(worker.endpoint, settings, "/login/verify" if login_session else ("/probe" if inference else "/account"),
                                   {"session_id": login_session} if login_session else None)
        account = payload.get("account")
        if not account:
            raise WorkerFailure("Gemini account or inference unavailable", kind=payload.get("kind", "logged_out"))
        await update_account(worker, account)
        worker.status = WorkerStatus.ready
        worker.failure_kind = worker.failure_reason = worker.retry_after = worker.quarantined_at = None
        worker.last_seen_at = datetime.now(timezone.utc)
        ok, message = True, "Gemini 账号和模型访问检查通过"
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            return {"ok": False, "logged_in": bool(worker.auth_mode), "message": "Gemini 正在执行或登录，请稍后探测"}
        worker.status = WorkerStatus.error
        worker.failure_kind = "connection"
        worker.retry_after = datetime.now(timezone.utc) + timedelta(seconds=settings.worker_failure_cooldown_seconds)
        ok, message = False, "Gemini 服务暂时不可用"
    except Exception as exc:
        kind = exc.kind if isinstance(exc, WorkerFailure) else classify_worker_failure(str(exc))
        worker.status = WorkerStatus.error
        worker.failure_kind = kind
        if kind == "logged_out":
            await update_account(worker, None)
        worker.failure_reason = "Gemini account check failed"
        worker.retry_after = datetime.now(timezone.utc) + timedelta(seconds=settings.worker_limit_cooldown_seconds if kind == "limit" else settings.worker_failure_cooldown_seconds)
        ok, message = False, "Gemini 检查失败，请检查登录状态或稍后重试"
    await reconcile_worker(db, worker)
    await db.commit()
    return {"ok": ok, "logged_in": ok, "message": message}
