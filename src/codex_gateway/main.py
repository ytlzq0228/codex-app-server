import json
import hashlib
import logging
from decimal import Decimal
import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from .admin import auth_router as admin_auth_router
from .admin import probe_worker_record, router as admin_router
from .admin import user_router as admin_user_router
from .auth import ApiPrincipal, require_api_key
from .app_server import open_app_server
from .providers import provider_for, validate_capabilities, validate_chat_capabilities, reject
from .gemini_backend import ProviderBackend
from .backend import AppServerBackend, BackendTarget, CompletionBackend, MockBackend, WorkerFailure, classify_worker_failure, run_healthcheck_turn
from .billing import priced_amount
from .config import get_settings
from .database import SessionLocal, engine, get_session
from .models import MetricSnapshot, ModelPrice, Base, ResponseBinding, UsageRecord, Worker, WorkerStatus
from .audit import RequestAuditMiddleware, current_audit, request_params as captured_params
from .request_observation import request_observation
from .conversations import correlate, durable_write
from .client_tools import ToolProtocolError
from .grammar_tools import validate_grammars
from .execution import prepare as prepare_execution, finish as finish_execution
from .quota import reconcile_worker, enforce_quota
from .contributions import account_monitor_loop, update_account
from .migrations import upgrade, bootstrap_users
from .schemas import BackendResult, ChatCompletionRequest, ResponseRequest

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.auto_create_schema:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            # This project intentionally has no migration framework yet. Keep existing
            # installations forward-compatible until Alembic is introduced.
            if connection.dialect.name == "postgresql":
                for ddl in (
                    "ALTER TABLE workers ADD COLUMN IF NOT EXISTS failure_kind VARCHAR(32)",
                    "ALTER TABLE workers ADD COLUMN IF NOT EXISTS failure_reason VARCHAR(500)",
                    "ALTER TABLE workers ADD COLUMN IF NOT EXISTS quarantined_at TIMESTAMPTZ",
                    "ALTER TABLE workers ADD COLUMN IF NOT EXISTS retry_after TIMESTAMPTZ",
                    "ALTER TABLE workers ADD COLUMN IF NOT EXISTS recovered_at TIMESTAMPTZ",
                    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
                    "CREATE INDEX IF NOT EXISTS ix_api_keys_deleted_at ON api_keys (deleted_at)",
                    "ALTER TABLE response_bindings ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ",
                    "ALTER TABLE response_bindings ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
                    "ALTER TABLE response_bindings ADD COLUMN IF NOT EXISTS status VARCHAR(16)",
                    "ALTER TABLE response_bindings ADD COLUMN IF NOT EXISTS invalid_reason VARCHAR(500)",
                    "UPDATE response_bindings SET last_used_at = COALESCE(last_used_at, created_at), status = COALESCE(status, 'active')",
                    "CREATE INDEX IF NOT EXISTS ix_response_bindings_last_used_at ON response_bindings (last_used_at)",
                    "CREATE INDEX IF NOT EXISTS ix_response_bindings_expires_at ON response_bindings (expires_at)",
                    "CREATE INDEX IF NOT EXISTS ix_response_bindings_status ON response_bindings (status)",
                    "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS endpoint VARCHAR(32)",
                    "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS previous_response_id VARCHAR(80)",
                    "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS thread_id VARCHAR(120)",
                    "UPDATE usage_records SET endpoint = CASE WHEN request_id LIKE 'chatcmpl-%' THEN 'chat.completions' ELSE 'responses' END WHERE endpoint IS NULL",
                    "UPDATE usage_records AS usage SET thread_id = binding.thread_id FROM response_bindings AS binding WHERE usage.request_id = binding.response_id AND usage.thread_id IS NULL",
                    "CREATE INDEX IF NOT EXISTS ix_usage_records_endpoint ON usage_records (endpoint)",
                    "CREATE INDEX IF NOT EXISTS ix_usage_records_previous_response_id ON usage_records (previous_response_id)",
                    "CREATE INDEX IF NOT EXISTS ix_usage_records_thread_id ON usage_records (thread_id)",
                ):
                    await connection.execute(text(ddl))
                await upgrade(connection)
    async with SessionLocal() as session:
        await bootstrap_users(session, settings)
        worker = await session.scalar(select(Worker).where(Worker.name == "worker-1"))
        if not worker:
            worker = Worker(owner_username=settings.admin_username, name="worker-1", container_name="codex-worker-1", endpoint=settings.app_server_url, status=WorkerStatus.ready)
            session.add(worker)
        elif worker.endpoint != "removed://worker":
            worker.container_name = "codex-worker-1"
            worker.endpoint = settings.app_server_url
        # Apply contribution rule changes to existing users before serving traffic.
        from .models import User
        for username in (await session.scalars(select(User.username).order_by(User.username))).all():
            await enforce_quota(session, username)
        await session.commit()
    app.state.backend = MockBackend() if settings.backend == "mock" else ProviderBackend(settings)
    recovery_task = asyncio.create_task(worker_recovery_loop(), name="worker-recovery")
    contribution_task = asyncio.create_task(account_monitor_loop(), name="worker-contributions") if settings.backend != "mock" else None
    from .monitoring import monitoring_loop
    monitoring_task = asyncio.create_task(monitoring_loop(), name="monitoring-snapshots")
    yield
    monitoring_task.cancel()
    await asyncio.gather(monitoring_task, return_exceptions=True)
    if contribution_task:
        contribution_task.cancel()
        await asyncio.gather(contribution_task, return_exceptions=True)
    recovery_task.cancel()
    await asyncio.gather(recovery_task, return_exceptions=True)
    await app.state.backend.close()
    await engine.dispose()


PACKAGE_ROOT = Path(__file__).resolve().parent

app = FastAPI(title="Codex App Server Gateway", version="0.4.0", lifespan=lifespan)
app.state.templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")
app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
app.include_router(admin_auth_router)
app.include_router(admin_user_router)
app.include_router(admin_router)
from .self_service import router as self_service_router
from .reporting import router as reporting_router
app.include_router(self_service_router)
app.include_router(reporting_router)
from .contributions import router as contribution_router
app.include_router(contribution_router)
app.add_middleware(RequestAuditMiddleware)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def binding_expiry(now: datetime | None = None):
    # Retained for callers; ordinary execution bindings no longer time out.
    return None


async def expire_response_bindings(session: AsyncSession) -> int:
    return 0


async def touch_response_thread(session: AsyncSession, binding: ResponseBinding) -> None:
    now = utcnow()
    await session.execute(
        update(ResponseBinding)
        .where(
            ResponseBinding.api_key_id == binding.api_key_id,
            ResponseBinding.thread_id == binding.thread_id,
            ResponseBinding.status == "active",
        )
        .values(last_used_at=now, expires_at=binding_expiry(now))
    )


async def invalidate_response_thread(api_key_id, thread_id: str, reason: str) -> None:
    async with SessionLocal() as session:
        await session.execute(
            update(ResponseBinding)
            .where(ResponseBinding.api_key_id == api_key_id, ResponseBinding.thread_id == thread_id, ResponseBinding.status == "active")
            .values(status="broken", invalid_reason=reason[:500])
        )
        await session.commit()


async def quarantine_worker(worker_id, reason: str, kind: str = "connection") -> bool:
    """Confirm a suspected failure with the same inference probe used by Worker 管理."""
    settings = get_settings()
    async with SessionLocal() as session:
        worker = await session.scalar(select(Worker).where(Worker.id == worker_id).execution_options(populate_existing=True))
        if not worker:
            return False
        if (getattr(worker, "provider", None) or "codex") == "gemini":
            worker.status = WorkerStatus.error
            worker.failure_kind = kind
            worker.failure_reason = reason[:500]
            worker.quarantined_at = utcnow()
            worker.retry_after = utcnow() + timedelta(seconds=settings.worker_limit_cooldown_seconds if kind == "limit" else settings.worker_failure_cooldown_seconds)
            await reconcile_worker(session, worker)
            await session.commit()
            return True
        result = await probe_worker_record(worker, session, settings)
        if result["ok"]:
            logger.warning(
                "Worker failure rejected by inference probe: worker_id=%s reported_kind=%s reported_reason=%s",
                worker_id, kind, reason,
            )
            return False
        logger.warning(
            "Worker failure confirmed by inference probe: worker_id=%s reported_kind=%s reported_reason=%s final_kind=%s final_reason=%s",
            worker_id, kind, reason, worker.failure_kind, worker.failure_reason,
        )
        return True


async def recover_worker(worker: Worker) -> bool:
    if (getattr(worker, "provider", None) or "codex") == "gemini":
        from .gemini_backend import probe_gemini
        from sqlalchemy.ext.asyncio import async_object_session
        result = await probe_gemini(worker, async_object_session(worker), get_settings())
        if result["ok"]:
            worker.recovered_at = utcnow()
        return result["ok"]
    settings = get_settings()
    try:
        async with open_app_server(worker.endpoint, settings.app_server_token.get_secret_value(), min(settings.app_server_timeout_seconds, 20)) as server:
            response = await server.call("account/read", {"refreshToken": True})
            account = response.get("account") or {}
            await update_account(worker, account)
            if not account:
                worker.failure_kind = "logged_out"
                worker.failure_reason = "Codex worker is not logged in"
                worker.retry_after = utcnow() + timedelta(seconds=settings.worker_failure_cooldown_seconds)
                return False
            await run_healthcheck_turn(server, settings.upstream_model)
        worker.status = WorkerStatus.ready
        worker.auth_mode = account.get("type")
        worker.plan_type = account.get("planType")
        await update_account(worker, account)
        worker.last_seen_at = utcnow()
        worker.recovered_at = utcnow()
        worker.failure_kind = None
        worker.failure_reason = None
        worker.quarantined_at = None
        worker.retry_after = None
        return True
    except Exception as exc:
        kind = exc.kind if isinstance(exc, WorkerFailure) else classify_worker_failure(str(exc))
        worker.failure_kind = kind
        if kind == "logged_out":
            await update_account(worker, None)
        worker.failure_reason = str(exc)[:500]
        cooldown = settings.worker_limit_cooldown_seconds if kind == "limit" else settings.worker_failure_cooldown_seconds
        worker.retry_after = utcnow() + timedelta(seconds=cooldown)
        return False


async def worker_recovery_loop() -> None:
    settings = get_settings()
    while True:
        try:
            await asyncio.sleep(settings.worker_recovery_interval_seconds)
            async with SessionLocal() as session:
                await expire_response_bindings(session)
                workers = (await session.scalars(select(Worker).where(Worker.enabled.is_(True), ((Worker.status == WorkerStatus.error) | ((Worker.provider == "gemini") & (Worker.status == WorkerStatus.offline) & Worker.auth_mode.is_not(None)))).with_for_update(skip_locked=True))).all()
                now = utcnow()
                for worker in workers:
                    if not worker.retry_after or worker.retry_after <= now:
                        await recover_worker(worker)
                        await reconcile_worker(session, worker)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed sweep must not terminate future recovery attempts.
            continue


@app.middleware("http")
async def request_limits_and_headers(request: Request, call_next):
    request_id = f"req_{uuid4().hex}"
    request.state.request_id = request_id
    length = request.headers.get("content-length")
    try:
        too_large = bool(length and int(length) > get_settings().max_request_bytes)
    except ValueError:
        too_large = False
    response = openai_error(413, "Request body is too large", "request_too_large") if too_large else await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Request-Id"] = request_id
    return response


def openai_error(status_code: int, message: str, code: str, error_type: str = "invalid_request_error", param: str | None = None) -> JSONResponse:
    if audit := current_audit.get():
        audit["rejection"] = {"code": code, "message": message[:1000], "param": param}
    return JSONResponse(status_code=status_code, content={"error": {"message": message, "type": error_type, "code": code, "param": param}})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    error = exc.errors()[0]
    location = [str(part) for part in error.get("loc", ()) if part not in {"body", "query"}]
    param = ".".join(location) or None
    message = error.get("msg", "Invalid request")
    if message.startswith("Value error, "):
        message = message.removeprefix("Value error, ")
    return openai_error(400, message, "invalid_request", param=param)


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    if (
        exc.status_code == 403
        and "text/html" in request.headers.get("accept", "").lower()
        and request.headers.get("x-requested-with", "").lower() != "xmlhttprequest"
        and not request.url.path.startswith("/v1/")
    ):
        return request.app.state.templates.TemplateResponse(
            request, "errors/403.html", {}, status_code=403,
            headers={**(exc.headers or {}), "Cache-Control": "no-store"},
        )
    if isinstance(exc.detail, dict) and isinstance(exc.detail.get("error"), dict):
        error = exc.detail["error"]
        response = openai_error(
            exc.status_code,
            error.get("message", "Request failed"),
            error.get("code", "invalid_request"),
            error.get("type", "invalid_request_error"),
            error.get("param"),
        )
    else:
        response = openai_error(exc.status_code, str(exc.detail), "invalid_request")
    if exc.headers:
        response.headers.update(exc.headers)
    return response


@app.exception_handler(Exception)
async def backend_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # Do not expose container endpoints, credentials, or internal trace details.
    return openai_error(502, "The Codex backend could not complete the request", "backend_error", "server_error")


@app.exception_handler(ToolProtocolError)
async def tool_protocol_error_handler(_: Request, exc: ToolProtocolError):
    return openai_error(409 if exc.code == "conversation_waiting_tool" else 400, str(exc), exc.code, param="tools")


def get_backend(request: Request) -> CompletionBackend:
    return request.app.state.backend


def response_object(response_id: str, body: ResponseRequest, result: BackendResult, status: str = "completed", message_id: str | None = None, previous_response_id: str | None = None) -> dict:
    created_at = int(time.time())
    return {
        "id": response_id, "object": "response", "created_at": created_at, "completed_at": created_at if status == "completed" else None,
        "status": status, "model": body.model,
        "previous_response_id": previous_response_id,
        "output": ([{"id": message_id or f"msg_{uuid4().hex}", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": result.text, "annotations": []}]}] if result.text or not result.tool_calls else []) + result.tool_calls,
        "usage": {"input_tokens": result.input_tokens, "input_tokens_details": {"cached_tokens": result.cache_read_tokens, "cache_write_tokens": result.cache_write_tokens}, "output_tokens": result.output_tokens, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": result.input_tokens + result.output_tokens},
        "metadata": body.metadata or {}, "error": None, "incomplete_details": None,
        "instructions": body.instructions, "max_output_tokens": body.max_output_tokens,
        "parallel_tool_calls": body.parallel_tool_calls if body.parallel_tool_calls is not None else True,
        "reasoning": body.reasoning, "store": False,
        "temperature": body.temperature, "text": body.text or {"format": {"type": "text"}},
        "tool_choice": body.tool_choice or "auto", "tools": body.tools or [], "top_p": body.top_p,
        "truncation": body.truncation or "disabled", "service_tier": body.service_tier or "default",
    }


def sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"


def chat_sse(payload: dict | str) -> str:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"data: {data}\n\n"


def chat_completion_object(completion_id: str, created: int, body: ChatCompletionRequest, result: BackendResult) -> dict:
    payload = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": body.model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": result.text, "refusal": None}, "logprobs": None, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": result.input_tokens, "prompt_tokens_details": {"cached_tokens": result.cache_read_tokens, "cache_write_tokens": result.cache_write_tokens}, "completion_tokens": result.output_tokens, "total_tokens": result.input_tokens + result.output_tokens},
        "system_fingerprint": None,
    }
    if result.tool_calls:
        payload["choices"][0]["message"]["tool_calls"] = [{"id":c["call_id"], "type":"function", "function":{"name":c["name"], "arguments":c["arguments"]}} for c in result.tool_calls]
        payload["choices"][0]["finish_reason"] = "tool_calls"
    if body.service_tier:
        payload["service_tier"] = body.service_tier
    return payload


def worker_unavailable(*, bound: bool = False) -> HTTPException:
    message = "The worker for this conversation is unavailable" if bound else "No healthy Codex worker is available"
    code = "session_worker_unavailable" if bound else "worker_unavailable"
    return HTTPException(503, detail={"error": {"message": message, "type": "server_error", "code": code}})


def rank_pool_workers(candidates, active_connections, usage_payload, cache_scope=None):
    """Prefer low live load, then the greatest weighted weekly remainder."""
    ranked = list(candidates)
    if cache_scope:
        ranked.sort(key=lambda worker: hashlib.sha256(
            f"{cache_scope}:{worker.id}".encode()).digest(), reverse=True)
    usage_by_worker = usage_payload.get("workers", {}) if isinstance(usage_payload, dict) else {}

    def preference(worker):
        value = usage_by_worker.get(str(worker.id), {}).get("weighted_remaining")
        known = isinstance(value, (int, float)) and not isinstance(value, bool)
        return (active_connections.get(str(worker.id), 0), not known, -float(value) if known else 0.0)

    # Python's stable sort retains cache affinity (or the randomized DB order)
    # only after the load and weekly-capacity criteria tie.
    ranked.sort(key=preference)
    return ranked


async def pool_routing_signals(session: AsyncSession):
    backend = getattr(app.state, "backend", None)
    pool = getattr(backend, "pool", None)
    active = await pool.active_connections_by_worker() if pool and hasattr(pool, "active_connections_by_worker") else {}
    latest = await session.scalar(select(MetricSnapshot).where(
        MetricSnapshot.metric == "subscription_usage",
        MetricSnapshot.observed_at >= datetime.now(timezone.utc) - timedelta(hours=2),
    ).order_by(MetricSnapshot.bucket_at.desc()).limit(1))
    return active, latest.payload if latest else {}


async def choose_target(
    principal: ApiPrincipal,
    session: AsyncSession,
    binding: ResponseBinding | None = None,
    exclude_worker_ids: set | None = None,
    cache_affinity: str | None = None,
    provider: str = "codex",
) -> BackendTarget:
    settings = get_settings()
    excluded = exclude_worker_ids or set()
    if binding and (getattr(binding, "provider", None) or "codex") != provider:
        reject("model", "Continuation cannot change provider", "provider_mismatch")
    candidates: list[Worker] = []
    if binding:
        worker = await session.get(Worker, binding.worker_id)
        candidates = [worker] if worker else []
    elif principal.pinned_worker_id:
        worker = await session.get(Worker, principal.pinned_worker_id)
        candidates = [worker] if worker else []
    else:
        candidates = list((await session.scalars(select(Worker).where(Worker.provider == provider, Worker.enabled.is_(True), Worker.status.in_([WorkerStatus.ready, WorkerStatus.busy])).order_by(func.random()))).all())
        active, usage = await pool_routing_signals(session)
        scope = f"{principal.key_id or 'development'}:{cache_affinity}" if cache_affinity else None
        candidates = rank_pool_workers(candidates, active, usage, scope)
    candidates = [worker for worker in candidates if (getattr(worker, "provider", None) or "codex") == provider and worker.id not in excluded and worker.enabled and worker.status in {WorkerStatus.ready, WorkerStatus.busy}]
    if not candidates:
        raise worker_unavailable(bound=binding is not None)
    key_slug = str(principal.key_id or "development")
    for worker in candidates:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.put(f"{settings.manager_url}/workers/{worker.container_name}/workspaces/{key_slug}", headers={"Authorization": f"Bearer {settings.manager_token.get_secret_value()}"})
            if response.status_code >= 400:
                raise RuntimeError(f"Worker manager returned HTTP {response.status_code}")
            return BackendTarget(connection_key=f"{key_slug}:{worker.id}", endpoint=worker.endpoint, workspace=f"{settings.workspace_worker_root}/{key_slug}", worker_id=worker.id, worker_generation=worker.execution_generation, provider=getattr(worker, "provider", None) or "codex")
        except Exception as exc:
            await quarantine_worker(worker.id, f"Workspace preparation failed: {exc}", "connection")
            if binding or principal.pinned_worker_id:
                break
    raise worker_unavailable(bound=binding is not None)


async def validate_pending_worker(backend, target, session):
    if not target or not target.worker_id:
        return
    worker = await session.get(Worker, target.worker_id)
    if not worker or worker.endpoint == "removed://worker" or (target.worker_generation is not None and worker.execution_generation != target.worker_generation):
        sessions = getattr(backend, "tool_sessions", None)
        if sessions:
            for run in list(sessions.runs):
                if run.target == target:
                    await sessions.cancel_thread(target.connection_key.split(":", 1)[0], run.thread_id)
        raise ToolProtocolError("Worker account changed or Worker was removed; start a new user turn with full history", "tool_worker_invalidated")


def reset_auto_resume(body, reason):
    if not body._execution_auto_resume:
        return
    body.previous_response_id = None
    body._execution_input_text = None
    body._execution_input_items = None
    if audit := current_audit.get():
        audit.setdefault("execution_decision", {}).update(action="new_thread", reason=reason)


async def choose_execution_target(principal, session, binding, body):
    try:
        return await choose_target(principal, session, binding, cache_affinity=body.prompt_cache_key, provider=provider_for(body.model))
    except HTTPException:
        if not body._execution_auto_resume or principal.pinned_worker_id or provider_for(body.model) != "codex":
            raise
        reset_auto_resume(body, "bound_worker_unavailable")
        return await choose_target(principal, session, cache_affinity=body.prompt_cache_key, provider=provider_for(body.model))


async def retry_target(principal: ApiPrincipal, failed: BackendTarget) -> BackendTarget:
    async with SessionLocal() as session:
        return await choose_target(principal, session, exclude_worker_ids={failed.worker_id}, provider=failed.provider)


async def release_request_session(session: AsyncSession) -> None:
    """Do not hold a database connection while a long Codex turn is running."""
    if session.in_transaction():
        await session.commit()
    await session.close()


async def complete_with_failover(body: ResponseRequest, backend: CompletionBackend, principal: ApiPrincipal, target: BackendTarget, *, allow_retry: bool) -> tuple[BackendResult, BackendTarget]:
    try:
        return await backend.complete(body, target), target
    except WorkerFailure as exc:
        if exc.kind == "request":
            raise HTTPException(400, detail={"error": {"message": worker_failure_message(exc), "type": "invalid_request_error", "code": exc.__cause__.code if isinstance(exc.__cause__, ToolProtocolError) else "invalid_request", "param": "tools" if isinstance(exc.__cause__, ToolProtocolError) else "model"}}) from exc
        if exc.kind == "session":
            raise HTTPException(404, detail={"error": {"message": "The previous response session is no longer available", "type": "invalid_request_error", "code": "previous_response_not_found", "param": "previous_response_id"}}) from exc
        if exc.kind == "capacity":
            raise HTTPException(503, detail={"error": {"message": "Timed out waiting for an available Codex worker connection", "type": "server_error", "code": "worker_capacity_exceeded", "param": None}}, headers={"Retry-After": "5"}) from exc
        if target.worker_id and exc.kind != "account_changed":
            await quarantine_worker(target.worker_id, str(exc), exc.kind)
        if target.provider == "gemini" and exc.kind == "limit":
            raise HTTPException(429, detail={"error": {"message": "Gemini subscription quota is exhausted; retry later", "type": "rate_limit_error", "code": "provider_quota_exhausted", "param": None}}, headers={"Retry-After": "60"}) from exc
        if not (allow_retry and exc.safe_to_retry):
            raise
        reset_auto_resume(body, "worker_pre_turn_failure")
        replacement = await retry_target(principal, target)
        try:
            return await backend.complete(body, replacement), replacement
        except WorkerFailure as retry_exc:
            if retry_exc.kind not in {"request", "session", "capacity", "account_changed"} and replacement.worker_id:
                await quarantine_worker(replacement.worker_id, str(retry_exc), retry_exc.kind)
            raise


def worker_failure_message(exc: Exception) -> str:
    raw = str(exc)
    try:
        payload = json.loads(raw)
        return payload.get("error", {}).get("message") or raw
    except (ValueError, TypeError, AttributeError):
        return raw


@durable_write
async def save_usage(
    response_id: str,
    principal: ApiPrincipal,
    target: BackendTarget,
    model: str,
    status_code: int,
    started: float,
    result: BackendResult | None = None,
    error_code: str | None = None,
    *,
    request_params: dict | None = None,
    persist_binding: bool = False,
    previous_response_id: str | None = None,
    thread_id: str | None = None,
) -> None:
    async with SessionLocal() as session:
        endpoint = "chat.completions" if response_id.startswith("chatcmpl-") else "responses"
        audit = current_audit.get()
        if audit is not None:
            request_params = captured_params(audit)
        price = await session.get(ModelPrice, model)
        if audit is None and request_params is not None and endpoint == "responses":
            request_params = {**request_params, "previous_response_id": previous_response_id}
        cost = priced_amount(result.input_tokens, result.output_tokens, result.cache_read_tokens, result.cache_write_tokens, price) if price and result else (Decimal(0) if price else None)
        record = UsageRecord(provider=target.provider, owner_username=principal.owner_username, request_params=request_params, request_observation=request_observation(audit), input_price=price.input_price if price else None, output_price=price.output_price if price else None, cache_read_price=price.cache_read_price if price else None, cache_write_price=price.cache_write_price if price else None, cost_usd=cost, request_id=response_id, api_key_id=principal.key_id, worker_id=target.worker_id, model=model, status_code=status_code, input_tokens=result.input_tokens if result else 0, output_tokens=result.output_tokens if result else 0, cache_read_tokens=result.cache_read_tokens if result else 0, cache_write_tokens=result.cache_write_tokens if result else 0, duration_ms=int((time.monotonic() - started) * 1000), error_code=error_code, endpoint=endpoint, previous_response_id=previous_response_id, thread_id=thread_id or (result.thread_id if result else None))
        await correlate(session, record, result.text if result else None)
        if audit and audit.get("execution_decision"):
            record.conversation_evidence["execution"] = audit["execution_decision"]
            record.conversation_evidence["auto_resume"] = audit["execution_decision"]["action"] == "resume"
        if audit and audit.get("execution"):
            record.logical_conversation_id = audit["execution"]["logical_id"]
        if audit and audit.get("rejection"):
            record.conversation_evidence["rejection"] = audit["rejection"]
            record.error_code = audit["rejection"].get("code", record.error_code)
        if result and result.tool_calls:
            record.conversation_evidence["execution_outcome"] = "waiting_client_tool"
            record.conversation_evidence["client_tool_call_ids"] = [c["call_id"] for c in result.tool_calls]
        # Lock before inserting FK references, avoiding concurrent lock upgrades.
        worker = await session.scalar(select(Worker).where(Worker.id == target.worker_id).with_for_update()) if target.worker_id else None
        session.add(record)
        generation_ok = worker is None or target.worker_generation is None or worker.execution_generation == target.worker_generation
        checkpoint_ok = await finish_execution(session, audit, result if status_code == 200 and generation_ok else None, target, response_id)
        retired = await session.scalar(select(ResponseBinding.response_id).where(
            ResponseBinding.api_key_id == principal.key_id, ResponseBinding.thread_id == result.thread_id,
            ResponseBinding.status != "active").limit(1)) if result and principal.key_id else None
        if (persist_binding or (audit and audit.get("execution"))) and result and principal.key_id and result.thread_id and target.worker_id and generation_ok and checkpoint_ok and not retired:
            now = utcnow()
            session.add(ResponseBinding(response_id=response_id, api_key_id=principal.key_id, worker_id=target.worker_id, thread_id=result.thread_id, last_used_at=now, expires_at=None, status="active", worker_generation=worker.execution_generation, provider=getattr(worker, "provider", None) or "codex"))
        await session.commit()
        if audit is not None:
            audit["saved"] = True
            audit["persisted_request_id"] = response_id


async def response_stream(body: ResponseRequest, backend: CompletionBackend, principal: ApiPrincipal, target: BackendTarget, public_previous_id: str | None, allow_retry: bool = True) -> AsyncIterator[str]:
    started = time.monotonic()
    response_id, message_id, sequence = f"resp_{uuid4().hex}", f"msg_{uuid4().hex}", 0
    created = {"id": response_id, "object": "response", "created_at": int(time.time()), "completed_at": None, "status": "in_progress", "model": body.model, "previous_response_id": public_previous_id, "output": [], "error": None, "incomplete_details": None, "instructions": body.instructions, "metadata": body.metadata or {}, "max_output_tokens": body.max_output_tokens, "parallel_tool_calls": body.parallel_tool_calls if body.parallel_tool_calls is not None else True, "reasoning": body.reasoning, "store": False, "temperature": body.temperature, "text": body.text or {"format": {"type": "text"}}, "tool_choice": body.tool_choice or "auto", "tools": body.tools or [], "top_p": body.top_p, "truncation": body.truncation or "disabled", "usage": None}
    yield sse({"type": "response.created", "sequence_number": sequence, "response": created}); sequence += 1
    yield sse({"type": "response.in_progress", "sequence_number": sequence, "response": created}); sequence += 1
    message_started = False
    tool_calls = []
    chunks: list[str] = []
    thread_id = ""
    input_tokens = output_tokens = cache_read_tokens = cache_write_tokens = 0
    retried = False
    try:
        while True:
            try:
                async for event in backend.stream(body, target):
                    thread_id = event.thread_id or thread_id
                    input_tokens = event.input_tokens or input_tokens
                    output_tokens = event.output_tokens or output_tokens
                    cache_read_tokens = event.cache_read_tokens or cache_read_tokens
                    cache_write_tokens = event.cache_write_tokens or cache_write_tokens
                    if event.tool_call:
                        tool_calls.append(event.tool_call)
                    if event.delta:
                        if not message_started:
                            yield sse({"type": "response.output_item.added", "sequence_number": sequence, "output_index": 0, "item": {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}}); sequence += 1
                            yield sse({"type": "response.content_part.added", "sequence_number": sequence, "item_id": message_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}); sequence += 1
                            message_started = True
                        chunks.append(event.delta)
                        yield sse({"type": "response.output_text.delta", "sequence_number": sequence, "item_id": message_id, "output_index": 0, "content_index": 0, "delta": event.delta}); sequence += 1
                break
            except WorkerFailure as exc:
                if exc.kind not in {"request", "session", "capacity", "account_changed"} and target.worker_id:
                    await quarantine_worker(target.worker_id, str(exc), exc.kind)
                # Retry once only when Codex confirms the turn could not have begun and
                # no model content has reached the client.
                if retried or chunks or not allow_retry or not exc.safe_to_retry:
                    raise
                reset_auto_resume(body if isinstance(body, ResponseRequest) else request, "worker_pre_turn_failure")
                target = await retry_target(principal, target)
                retried = True
    except Exception as exc:
        session_failure = isinstance(exc, WorkerFailure) and exc.kind == "session"
        tool_failure = isinstance(exc, WorkerFailure) and isinstance(exc.__cause__, ToolProtocolError)
        capacity_failure = isinstance(exc, WorkerFailure) and exc.kind == "capacity"
        logger.exception("Responses backend failed: request_id=%s worker_id=%s failure_kind=%s", response_id,
                         target.worker_id, exc.kind if isinstance(exc, WorkerFailure) else type(exc).__name__)
        if session_failure and principal.key_id and public_previous_id:
            async with SessionLocal() as session:
                previous = await session.scalar(select(ResponseBinding).where(ResponseBinding.response_id == public_previous_id, ResponseBinding.api_key_id == principal.key_id))
                if previous:
                    await invalidate_response_thread(previous.api_key_id, previous.thread_id, str(exc))
        error = {
            "code": exc.__cause__.code if tool_failure else "previous_response_not_found" if session_failure else "worker_capacity_exceeded" if capacity_failure else "backend_error",
            "message": str(exc) if tool_failure else "The previous response session is no longer available" if session_failure else "Timed out waiting for an available Codex worker connection" if capacity_failure else "The Codex backend could not complete the request",
        }
        if target.provider == "gemini":
            error = {"code": "provider_quota_exhausted" if isinstance(exc, WorkerFailure) and exc.kind == "limit" else error["code"], "message": "Gemini subscription quota is exhausted" if isinstance(exc, WorkerFailure) and exc.kind == "limit" else "Gemini could not complete the request"}
        if audit := current_audit.get(): audit["rejection"] = error
        await save_usage(response_id, principal, target, body.model, 400 if tool_failure else 404 if session_failure else 503 if capacity_failure else 502, started, error_code=error["code"], previous_response_id=public_previous_id, thread_id=body.previous_response_id, request_params=body.model_dump(mode="json"))
        yield sse({"type": "error", "sequence_number": sequence, **error, "param": None}); sequence += 1
        failed = {**created, "status": "failed", "error": error}
        yield sse({"type": "response.failed", "sequence_number": sequence, "response": failed})
        return
    text = "".join(chunks).rstrip()
    result = BackendResult(text=text, tool_calls=tool_calls, thread_id=thread_id, input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens)
    await save_usage(response_id, principal, target, body.model, 200, started, result, persist_binding=True, previous_response_id=public_previous_id, request_params=body.model_dump(mode="json"))
    if message_started or not tool_calls:
        if not message_started:
            yield sse({"type": "response.output_item.added", "sequence_number": sequence, "output_index": 0, "item": {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}}); sequence += 1
            yield sse({"type": "response.content_part.added", "sequence_number": sequence, "item_id": message_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}); sequence += 1
        yield sse({"type": "response.output_text.done", "sequence_number": sequence, "item_id": message_id, "output_index": 0, "content_index": 0, "text": text}); sequence += 1
        part = {"type": "output_text", "text": text, "annotations": []}
        yield sse({"type": "response.content_part.done", "sequence_number": sequence, "item_id": message_id, "output_index": 0, "content_index": 0, "part": part}); sequence += 1
        item = {"id": message_id, "type": "message", "status": "completed", "role": "assistant", "content": [part]}
        yield sse({"type": "response.output_item.done", "sequence_number": sequence, "output_index": 0, "item": item}); sequence += 1
    for index, call in enumerate(tool_calls, start=1 if message_started else 0):
        argument_key = "arguments" if call["type"] == "function_call" else "input"
        event_name = "response.function_call_arguments" if argument_key == "arguments" else "response.custom_tool_call_input"
        yield sse({"type":"response.output_item.added", "sequence_number":sequence, "output_index":index, "item":{**call, "status":"in_progress", argument_key:""}}); sequence += 1
        yield sse({"type":event_name+".delta", "sequence_number":sequence, "output_index":index, "item_id":call["id"], "delta":call[argument_key]}); sequence += 1
        yield sse({"type":event_name+".done", "sequence_number":sequence, "output_index":index, "item_id":call["id"], argument_key:call[argument_key]}); sequence += 1
        yield sse({"type":"response.output_item.done", "sequence_number":sequence, "output_index":index, "item":call}); sequence += 1
    yield sse({"type": "response.completed", "sequence_number": sequence, "response": response_object(response_id, body, result, message_id=message_id, previous_response_id=public_previous_id)})


async def chat_completion_stream(body: ChatCompletionRequest, request: ResponseRequest, backend: CompletionBackend, principal: ApiPrincipal, target: BackendTarget, allow_retry: bool = True) -> AsyncIterator[str]:
    started = time.monotonic()
    completion_id, created = f"chatcmpl-{uuid4().hex}", int(time.time())

    def chunk(delta: dict, finish_reason: str | None = None, usage: dict | None = None) -> dict:
        payload = {
            "id": completion_id, "object": "chat.completion.chunk", "created": created, "model": body.model,
            "choices": [] if usage is not None else [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}],
            "system_fingerprint": None,
            "usage": usage,
        }
        if body.service_tier:
            payload["service_tier"] = body.service_tier
        return payload

    yield chat_sse(chunk({"role": "assistant", "content": ""}))
    thread_id = ""
    input_tokens = output_tokens = cache_read_tokens = cache_write_tokens = 0
    output_chunks = []
    tool_calls = []
    content_emitted = False
    retried = False
    try:
        while True:
            try:
                async for event in backend.stream(request, target):
                    thread_id = event.thread_id or thread_id
                    input_tokens = event.input_tokens or input_tokens
                    output_tokens = event.output_tokens or output_tokens
                    cache_read_tokens = event.cache_read_tokens or cache_read_tokens
                    cache_write_tokens = event.cache_write_tokens or cache_write_tokens
                    if event.tool_call:
                        tool_calls.append(event.tool_call)
                    if event.delta:
                        output_chunks.append(event.delta)
                        content_emitted = True
                        yield chat_sse(chunk({"content": event.delta}))
                break
            except WorkerFailure as exc:
                if exc.kind not in {"request", "session", "capacity", "account_changed"} and target.worker_id:
                    await quarantine_worker(target.worker_id, str(exc), exc.kind)
                if retried or content_emitted or not allow_retry or not exc.safe_to_retry:
                    raise
                reset_auto_resume(body if isinstance(body, ResponseRequest) else request, "worker_pre_turn_failure")
                target = await retry_target(principal, target)
                retried = True
    except Exception as exc:
        tool_failure = isinstance(exc, WorkerFailure) and isinstance(exc.__cause__, ToolProtocolError)
        capacity_failure = isinstance(exc, WorkerFailure) and exc.kind == "capacity"
        error_code = exc.__cause__.code if tool_failure else "worker_capacity_exceeded" if capacity_failure else "backend_error"
        error_message = str(exc) if tool_failure else "Timed out waiting for an available Codex worker connection" if capacity_failure else "The Codex backend could not complete the request"
        if target.provider == "gemini":
            error_code = "provider_quota_exhausted" if isinstance(exc, WorkerFailure) and exc.kind == "limit" else error_code
            error_message = "Gemini subscription quota is exhausted" if error_code == "provider_quota_exhausted" else "Gemini could not complete the request"
        if audit := current_audit.get(): audit["rejection"] = {"code": error_code, "message": error_message}
        await save_usage(completion_id, principal, target, body.model, 400 if tool_failure else 503 if capacity_failure else 502, started, error_code=error_code, request_params=body.model_dump(mode="json"))
        yield chat_sse({"error": {"message": error_message, "type": "invalid_request_error" if tool_failure else "server_error", "code": error_code}})
        yield chat_sse("[DONE]")
        return
    result = BackendResult(text="".join(output_chunks), tool_calls=tool_calls, thread_id=thread_id, input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens)
    await save_usage(completion_id, principal, target, body.model, 200, started, result, request_params=body.model_dump(mode="json"))
    for index, call in enumerate(tool_calls):
        yield chat_sse(chunk({"tool_calls":[{"index":index,"id":call["call_id"],"type":"function","function":{"name":call["name"],"arguments":call["arguments"]}}]}))
    yield chat_sse(chunk({}, "tool_calls" if tool_calls else "stop"))
    if body.stream_options and body.stream_options.include_usage:
        yield chat_sse(chunk({}, usage={"prompt_tokens": input_tokens,
            "prompt_tokens_details": {"cached_tokens": cache_read_tokens, "cache_write_tokens": cache_write_tokens},
            "completion_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}))
    yield chat_sse("[DONE]")


@app.middleware("http")
async def legacy_user_routes(request: Request, call_next):
    # Preserve bookmarked URLs, form methods and existing Google callback URLs.
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    path = request.url.path
    legacy_root_auth = ("/login", "/logout")
    legacy_user_auth = ("/user/login", "/user/logout")
    if any(path == root or path.startswith(root + "/") for root in legacy_root_auth):
        target = "/auth" + quote(path, safe="/")
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=307)
    if any(path == root or path.startswith(root + "/") for root in legacy_user_auth):
        target = "/auth" + quote(path.removeprefix("/user"), safe="/")
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=307)
    if path == "/user/auth/google" or path.startswith("/user/auth/google/"):
        target = quote(path.removeprefix("/user"), safe="/")
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=307)
    roots = ("/account", "/overview", "/workers", "/usage", "/debug")
    if any(path == root or path.startswith(root + "/") for root in roots):
        target = "/user" + quote(path, safe="/")
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=307)
    return await call_next(request)


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models(principal: ApiPrincipal = Depends(require_api_key), session: AsyncSession = Depends(get_session)) -> dict:
    from .providers import visible_models
    public_models = await visible_models(session, principal)
    return {"object": "list", "data": [{"id": model, "object": "model", "created": 0, "owned_by": "codex-gateway" if provider_for(model) == "codex" else provider_for(model)} for model in public_models]}


@app.get("/v1/models/{model_id}")
async def retrieve_model(model_id: str, principal: ApiPrincipal = Depends(require_api_key), session: AsyncSession = Depends(get_session)):
    if model_id not in get_settings().public_models():
        return openai_error(404, f"The model '{model_id}' does not exist", "model_not_found", param="model")
    from .providers import authorize_model
    await authorize_model(session, principal, model_id)
    return {"id": model_id, "object": "model", "created": 0, "owned_by": "codex-gateway" if provider_for(model_id) == "codex" else provider_for(model_id)}


@app.post("/v1/chat/completions")
async def create_chat_completion(body: ChatCompletionRequest, principal: ApiPrincipal = Depends(require_api_key), backend: CompletionBackend = Depends(get_backend), session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    if body.model not in settings.public_models():
        return openai_error(400, f"Model '{body.model}' is not available", "model_not_found", param="model")
    from .providers import authorize_model
    await authorize_model(session, principal, body.model)
    if unsupported := body.unsupported():
        return openai_error(400, unsupported[1], "unsupported_parameter", param=unsupported[0])
    validate_chat_capabilities(body)
    request = body.to_response_request()
    validate_capabilities(request)
    await validate_grammars(request)
    pending_target = backend.continuation_target(request, principal.key_id) if hasattr(backend, "continuation_target") else None
    await validate_pending_worker(backend, pending_target, session)
    pending_thread = backend.continuation_thread(request, principal.key_id) if pending_target and hasattr(backend, "continuation_thread") else None
    request, execution_binding = await prepare_execution(request, principal, "chat.completions", current_audit.get(), pending_thread=pending_thread, tool_sessions=getattr(backend,"tool_sessions",None))
    target = pending_target or await choose_execution_target(principal, session, execution_binding, request)
    allow_retry = not pending_target and (execution_binding is None or (request._execution_auto_resume and target.provider == "codex")) and principal.pinned_worker_id is None
    await release_request_session(session)
    if body.stream:
        return StreamingResponse(chat_completion_stream(body, request, backend, principal, target, allow_retry), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    started = time.monotonic()
    completion_id, created = f"chatcmpl-{uuid4().hex}", int(time.time())
    try:
        result, target = await complete_with_failover(request, backend, principal, target, allow_retry=allow_retry)
    except HTTPException as exc:
        if audit := current_audit.get(): audit["rejection"] = exc.detail.get("error", {}) if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        await save_usage(completion_id, principal, target, body.model, exc.status_code, started, error_code="invalid_request", request_params=body.model_dump(mode="json"))
        raise
    except Exception:
        await save_usage(completion_id, principal, target, body.model, 502, started, error_code="backend_error", request_params=body.model_dump(mode="json"))
        raise
    await save_usage(completion_id, principal, target, body.model, 200, started, result, request_params=body.model_dump(mode="json"))
    return chat_completion_object(completion_id, created, body, result)


@app.post("/v1/responses")
async def create_response(body: ResponseRequest, principal: ApiPrincipal = Depends(require_api_key), backend: CompletionBackend = Depends(get_backend), session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    if body.model not in settings.public_models():
        return openai_error(400, f"Model '{body.model}' is not available", "model_not_found", param="model")
    from .providers import authorize_model
    await authorize_model(session, principal, body.model)
    if unsupported := body.unsupported():
        return openai_error(400, unsupported[1], "unsupported_parameter", param=unsupported[0])
    validate_capabilities(body)
    await validate_grammars(body)
    public_previous_id = body.previous_response_id
    binding = None
    if public_previous_id:
        if not principal.key_id:
            return openai_error(400, "previous_response_id requires a persisted API key", "invalid_previous_response_id")
        binding = await session.scalar(select(ResponseBinding).where(ResponseBinding.response_id == public_previous_id, ResponseBinding.api_key_id == principal.key_id))
        if not binding:
            return openai_error(404, "previous_response_id was not found", "previous_response_not_found")
        if (getattr(binding, "provider", None) or "codex") != provider_for(body.model):
            reject("model", "Continuation cannot change provider", "provider_mismatch")
        worker = await session.get(Worker, binding.worker_id)
        if worker and (getattr(worker, "provider", None) or "codex") != (getattr(binding, "provider", None) or "codex"):
            reject("previous_response_id", "Worker provider changed", "provider_mismatch")
        if binding.status != "active" or not worker or binding.worker_generation != worker.execution_generation:
            return openai_error(404, "previous_response_id is no longer available", "previous_response_not_found", param="previous_response_id")
        await touch_response_thread(session, binding)
        body = body.model_copy(update={"previous_response_id": binding.thread_id})
    pending_target = backend.continuation_target(body, principal.key_id) if hasattr(backend, "continuation_target") else None
    if pending_target and binding and pending_target.worker_id != binding.worker_id:
        return openai_error(400, "Tool output and previous_response_id refer to different Workers", "invalid_client_tool")
    await validate_pending_worker(backend, pending_target, session)
    pending_thread = backend.continuation_thread(body, principal.key_id) if pending_target and hasattr(backend, "continuation_thread") else None
    body, binding = await prepare_execution(body, principal, "responses", current_audit.get(), pending_thread=pending_thread, binding=binding, tool_sessions=getattr(backend,"tool_sessions",None))
    target = pending_target or await choose_execution_target(principal, session, binding, body)
    await release_request_session(session)
    if body.stream:
        return StreamingResponse(response_stream(body, backend, principal, target, public_previous_id, not pending_target and (binding is None or (body._execution_auto_resume and target.provider == "codex")) and principal.pinned_worker_id is None), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    started = time.monotonic()
    try:
        result, target = await complete_with_failover(body, backend, principal, target, allow_retry=not pending_target and (binding is None or (body._execution_auto_resume and target.provider == "codex")) and principal.pinned_worker_id is None)
    except HTTPException as exc:
        if audit := current_audit.get(): audit["rejection"] = exc.detail.get("error", {}) if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        response_id = f"resp_{uuid4().hex}"
        if binding and exc.status_code == 404:
            await invalidate_response_thread(binding.api_key_id, binding.thread_id, "Codex thread could not be resumed")
        await save_usage(response_id, principal, target, body.model, exc.status_code, started, error_code="invalid_request", previous_response_id=public_previous_id, thread_id=body.previous_response_id, request_params=body.model_dump(mode="json"))
        raise
    except Exception:
        response_id = f"resp_{uuid4().hex}"
        await save_usage(response_id, principal, target, body.model, 502, started, error_code="backend_error", previous_response_id=public_previous_id, thread_id=body.previous_response_id, request_params=body.model_dump(mode="json"))
        raise
    response_id = f"resp_{uuid4().hex}"
    await save_usage(response_id, principal, target, body.model, 200, started, result, persist_binding=True, previous_response_id=public_previous_id, request_params=body.model_dump(mode="json"))
    return response_object(response_id, body, result, previous_response_id=public_previous_id)
