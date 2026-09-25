import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .admin import router as admin_router
from .auth import ApiPrincipal, require_api_key
from .backend import AppServerBackend, BackendTarget, CompletionBackend, MockBackend
from .config import get_settings
from .database import SessionLocal, engine, get_session
from .models import Base, ResponseBinding, UsageRecord, Worker, WorkerStatus
from .schemas import BackendResult, ChatCompletionRequest, ResponseRequest


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.auto_create_schema:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        worker = await session.scalar(select(Worker).where(Worker.name == "worker-1"))
        if not worker:
            worker = Worker(name="worker-1", container_name="codex-worker-1", endpoint=settings.app_server_url, status=WorkerStatus.ready)
            session.add(worker)
        else:
            worker.container_name = "codex-worker-1"
            worker.endpoint = settings.app_server_url
        await session.commit()
    app.state.backend = MockBackend() if settings.backend == "mock" else AppServerBackend(settings)
    yield
    await app.state.backend.close()
    await engine.dispose()


app = FastAPI(title="Codex App Server Gateway", version="0.3.0", lifespan=lifespan)
app.include_router(admin_router)


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
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
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


def get_backend(request: Request) -> CompletionBackend:
    return request.app.state.backend


def response_object(response_id: str, body: ResponseRequest, result: BackendResult, status: str = "completed", message_id: str | None = None, previous_response_id: str | None = None) -> dict:
    created_at = int(time.time())
    return {
        "id": response_id, "object": "response", "created_at": created_at, "completed_at": created_at if status == "completed" else None,
        "status": status, "model": body.model,
        "previous_response_id": previous_response_id,
        "output": [{"id": message_id or f"msg_{uuid4().hex}", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": result.text, "annotations": []}]}],
        "usage": {"input_tokens": result.input_tokens, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": result.output_tokens, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": result.input_tokens + result.output_tokens},
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
        "usage": {"prompt_tokens": result.input_tokens, "completion_tokens": result.output_tokens, "total_tokens": result.input_tokens + result.output_tokens},
        "system_fingerprint": None,
    }
    if body.service_tier:
        payload["service_tier"] = body.service_tier
    return payload


async def choose_target(principal: ApiPrincipal, session: AsyncSession, binding: ResponseBinding | None = None) -> BackendTarget:
    settings = get_settings()
    worker: Worker | None
    if binding:
        worker = await session.get(Worker, binding.worker_id)
    elif principal.pinned_worker_id:
        worker = await session.get(Worker, principal.pinned_worker_id)
    else:
        worker = await session.scalar(select(Worker).where(Worker.enabled.is_(True), Worker.status.in_([WorkerStatus.ready, WorkerStatus.busy])).order_by(Worker.status, Worker.last_seen_at.desc().nullslast()))
    if not worker or not worker.enabled or worker.status in {WorkerStatus.offline, WorkerStatus.draining, WorkerStatus.error}:
        raise HTTPException(503, detail={"error": {"message": "No healthy Codex worker is available", "type": "server_error", "code": "worker_unavailable"}})
    key_slug = str(principal.key_id or "development")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.put(f"{settings.manager_url}/workers/{worker.container_name}/workspaces/{key_slug}", headers={"Authorization": f"Bearer {settings.manager_token.get_secret_value()}"})
        if response.status_code >= 400:
            raise HTTPException(503, detail={"error": {"message": "Worker workspace could not be prepared", "type": "server_error", "code": "worker_unavailable"}})
    return BackendTarget(connection_key=f"{key_slug}:{worker.id}", endpoint=worker.endpoint, workspace=f"{settings.workspace_worker_root}/{key_slug}", worker_id=worker.id)


async def save_usage(response_id: str, principal: ApiPrincipal, target: BackendTarget, model: str, status_code: int, started: float, result: BackendResult | None = None, error_code: str | None = None) -> None:
    async with SessionLocal() as session:
        session.add(UsageRecord(request_id=response_id, api_key_id=principal.key_id, worker_id=target.worker_id, model=model, status_code=status_code, input_tokens=result.input_tokens if result else 0, output_tokens=result.output_tokens if result else 0, duration_ms=int((time.monotonic() - started) * 1000), error_code=error_code))
        if result and principal.key_id and result.thread_id and target.worker_id:
            session.add(ResponseBinding(response_id=response_id, api_key_id=principal.key_id, worker_id=target.worker_id, thread_id=result.thread_id))
        await session.commit()


async def response_stream(body: ResponseRequest, backend: CompletionBackend, principal: ApiPrincipal, target: BackendTarget, public_previous_id: str | None) -> AsyncIterator[str]:
    started = time.monotonic()
    response_id, message_id, sequence = f"resp_{uuid4().hex}", f"msg_{uuid4().hex}", 0
    created = {"id": response_id, "object": "response", "created_at": int(time.time()), "completed_at": None, "status": "in_progress", "model": body.model, "previous_response_id": public_previous_id, "output": [], "error": None, "incomplete_details": None, "instructions": body.instructions, "metadata": body.metadata or {}, "max_output_tokens": body.max_output_tokens, "parallel_tool_calls": body.parallel_tool_calls if body.parallel_tool_calls is not None else True, "reasoning": body.reasoning, "store": False, "temperature": body.temperature, "text": body.text or {"format": {"type": "text"}}, "tool_choice": body.tool_choice or "auto", "tools": body.tools or [], "top_p": body.top_p, "truncation": body.truncation or "disabled", "usage": None}
    yield sse({"type": "response.created", "sequence_number": sequence, "response": created}); sequence += 1
    yield sse({"type": "response.in_progress", "sequence_number": sequence, "response": created}); sequence += 1
    yield sse({"type": "response.output_item.added", "sequence_number": sequence, "output_index": 0, "item": {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}}); sequence += 1
    yield sse({"type": "response.content_part.added", "sequence_number": sequence, "item_id": message_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}); sequence += 1
    chunks: list[str] = []
    thread_id = ""
    input_tokens = output_tokens = 0
    try:
        async for event in backend.stream(body, target):
            thread_id = event.thread_id or thread_id
            input_tokens = event.input_tokens or input_tokens
            output_tokens = event.output_tokens or output_tokens
            if event.delta:
                chunks.append(event.delta)
                yield sse({"type": "response.output_text.delta", "sequence_number": sequence, "item_id": message_id, "output_index": 0, "content_index": 0, "delta": event.delta}); sequence += 1
    except Exception:
        error = {"code": "backend_error", "message": "The Codex backend could not complete the request"}
        yield sse({"type": "error", "sequence_number": sequence, **error, "param": None}); sequence += 1
        failed = {**created, "status": "failed", "error": error}
        yield sse({"type": "response.failed", "sequence_number": sequence, "response": failed})
        await save_usage(response_id, principal, target, body.model, 502, started, error_code="backend_error")
        return
    text = "".join(chunks).rstrip()
    yield sse({"type": "response.output_text.done", "sequence_number": sequence, "item_id": message_id, "output_index": 0, "content_index": 0, "text": text}); sequence += 1
    part = {"type": "output_text", "text": text, "annotations": []}
    yield sse({"type": "response.content_part.done", "sequence_number": sequence, "item_id": message_id, "output_index": 0, "content_index": 0, "part": part}); sequence += 1
    item = {"id": message_id, "type": "message", "status": "completed", "role": "assistant", "content": [part]}
    yield sse({"type": "response.output_item.done", "sequence_number": sequence, "output_index": 0, "item": item}); sequence += 1
    result = BackendResult(text=text, thread_id=thread_id, input_tokens=input_tokens, output_tokens=output_tokens)
    yield sse({"type": "response.completed", "sequence_number": sequence, "response": response_object(response_id, body, result, message_id=message_id, previous_response_id=public_previous_id)})
    await save_usage(response_id, principal, target, body.model, 200, started, result)


async def chat_completion_stream(body: ChatCompletionRequest, request: ResponseRequest, backend: CompletionBackend, principal: ApiPrincipal, target: BackendTarget) -> AsyncIterator[str]:
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
    input_tokens = output_tokens = 0
    try:
        async for event in backend.stream(request, target):
            thread_id = event.thread_id or thread_id
            input_tokens = event.input_tokens or input_tokens
            output_tokens = event.output_tokens or output_tokens
            if event.delta:
                yield chat_sse(chunk({"content": event.delta}))
    except Exception:
        yield chat_sse({"error": {"message": "The Codex backend could not complete the request", "type": "server_error", "code": "backend_error"}})
        yield chat_sse("[DONE]")
        await save_usage(completion_id, principal, target, body.model, 502, started, error_code="backend_error")
        return
    result = BackendResult(text="", thread_id=thread_id, input_tokens=input_tokens, output_tokens=output_tokens)
    yield chat_sse(chunk({}, "stop"))
    if body.stream_options and body.stream_options.include_usage:
        yield chat_sse(chunk({}, usage={"prompt_tokens": input_tokens, "completion_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}))
    yield chat_sse("[DONE]")
    await save_usage(completion_id, principal, target, body.model, 200, started, result)


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models(_: ApiPrincipal = Depends(require_api_key)) -> dict:
    settings = get_settings()
    return {"object": "list", "data": [{"id": model, "object": "model", "created": 0, "owned_by": "codex-gateway"} for model in settings.public_models()]}


@app.get("/v1/models/{model_id}")
async def retrieve_model(model_id: str, _: ApiPrincipal = Depends(require_api_key)):
    if model_id not in get_settings().public_models():
        return openai_error(404, f"The model '{model_id}' does not exist", "model_not_found", param="model")
    return {"id": model_id, "object": "model", "created": 0, "owned_by": "codex-gateway"}


@app.post("/v1/chat/completions")
async def create_chat_completion(body: ChatCompletionRequest, principal: ApiPrincipal = Depends(require_api_key), backend: CompletionBackend = Depends(get_backend), session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    if body.model not in settings.public_models():
        return openai_error(400, f"Model '{body.model}' is not available", "model_not_found", param="model")
    if unsupported := body.unsupported():
        return openai_error(400, unsupported[1], "unsupported_parameter", param=unsupported[0])
    request = body.to_response_request()
    target = await choose_target(principal, session)
    if body.stream:
        return StreamingResponse(chat_completion_stream(body, request, backend, principal, target), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    started = time.monotonic()
    completion_id, created = f"chatcmpl-{uuid4().hex}", int(time.time())
    try:
        result = await backend.complete(request, target)
    except Exception:
        await save_usage(completion_id, principal, target, body.model, 502, started, error_code="backend_error")
        raise
    await save_usage(completion_id, principal, target, body.model, 200, started, result)
    return chat_completion_object(completion_id, created, body, result)


@app.post("/v1/responses")
async def create_response(body: ResponseRequest, principal: ApiPrincipal = Depends(require_api_key), backend: CompletionBackend = Depends(get_backend), session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    if body.model not in settings.public_models():
        return openai_error(400, f"Model '{body.model}' is not available", "model_not_found", param="model")
    if unsupported := body.unsupported():
        return openai_error(400, unsupported[1], "unsupported_parameter", param=unsupported[0])
    public_previous_id = body.previous_response_id
    binding = None
    if public_previous_id:
        if not principal.key_id:
            return openai_error(400, "previous_response_id requires a persisted API key", "invalid_previous_response_id")
        binding = await session.scalar(select(ResponseBinding).where(ResponseBinding.response_id == public_previous_id, ResponseBinding.api_key_id == principal.key_id))
        if not binding:
            return openai_error(404, "previous_response_id was not found", "previous_response_not_found")
        body = body.model_copy(update={"previous_response_id": binding.thread_id})
    target = await choose_target(principal, session, binding)
    if body.stream:
        return StreamingResponse(response_stream(body, backend, principal, target, public_previous_id), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    started = time.monotonic()
    try:
        result = await backend.complete(body, target)
    except Exception:
        response_id = f"resp_{uuid4().hex}"
        await save_usage(response_id, principal, target, body.model, 502, started, error_code="backend_error")
        raise
    response_id = f"resp_{uuid4().hex}"
    await save_usage(response_id, principal, target, body.model, 200, started, result)
    return response_object(response_id, body, result, previous_response_id=public_previous_id)
