import asyncio
import json
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException

from codex_gateway.backend import BackendTarget, WorkerFailure
from codex_gateway.client_tools import ToolProtocolError
from codex_gateway.config import get_settings
from codex_gateway.gemini_backend import ProviderBackend
from codex_gateway.providers import validate_capabilities, validate_chat_capabilities
from codex_gateway.schemas import ResponseRequest, ChatCompletionRequest

TOOL = {"type": "function", "name": "lookup", "parameters": {
    "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}
TARGET = BackendTarget("key:worker", "http://worker", "/workspace/key", provider="claude")
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlS8AAAAASUVORK5CYII="


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_continuous_usage_survives_worker_failure(backend, monkeypatch, structured):
    from codex_gateway.audit import current_audit
    async def handler(request):
        lines = [dict(event="usage", thread_id="thread", input_tokens=120, output_tokens=8,
                      cache_read_tokens=20, cache_write_tokens=10),
                 dict(error="Claude execution interrupted or timed out: TimeoutError", kind="connection")]
        return httpx.Response(200, text="\n".join(json.dumps(line) for line in lines))
    transport(monkeypatch, handler)
    audit = {}
    token = current_audit.set(audit)
    try:
        request = ResponseRequest(model="claude-test", input="hi",
            text={"format": {"type": "json_schema", "name": "answer", "schema": {"type": "object"}}} if structured else None)
        with pytest.raises(WorkerFailure):
            await backend.complete(request, TARGET)
        assert audit["claude_usage"]["tokens"] == dict(input_tokens=120, output_tokens=8, cache_read_tokens=20, cache_write_tokens=10)
        assert audit["claude_usage"]["run_id"] and not audit["claude_usage"]["final"]
        assert audit["backend_context"]["thread_id"] == "thread"
    finally:
        current_audit.reset(token)
        await backend.close()


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(get_settings(), "model_providers", "claude-test:claude")
    return ProviderBackend(get_settings())


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_claude_capabilities(backend, effort):
    request = ResponseRequest(model="claude-test", input="hi", reasoning={"effort": effort},
        temperature=.1, top_p=.8, max_output_tokens=10, truncation="auto",
        service_tier="auto", prompt_cache_retention="24h", max_tool_calls=1, include=[], parallel_tool_calls=True)
    validate_capabilities(request)
    assert request.parallel_tool_calls is False
    chat = ChatCompletionRequest(model="claude-test", messages=[{"role": "user", "content": "hi"}],
                                 reasoning_effort=effort, stop=["STOP"], max_tokens=1)
    validate_chat_capabilities(chat)
    validate_capabilities(chat.to_response_request())


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status,kind", [
    ("worker_capacity_exceeded", 503, "capacity"),
    ("worker_queue_timeout", 503, "capacity"),
    ("worker_execution_conflict", 409, "execution_conflict"),
])
async def test_admission_errors_preserve_reason(backend, monkeypatch, code, status, kind):
    async def handler(request):
        return httpx.Response(status, json={"detail": {"code": code}})
    transport(monkeypatch, handler)
    with pytest.raises(WorkerFailure) as caught:
        await backend.complete(ResponseRequest(model="claude-test", input="hi"), TARGET)
    assert (caught.value.code, caught.value.status, caught.value.kind) == (code, status, kind)
    assert not caught.value.safe_to_retry
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("exception,code", [
    (httpx.ConnectTimeout, "worker_connection_timeout"),
    (httpx.ReadTimeout, "worker_transport_timeout"),
])
async def test_timeout_reasons(backend, monkeypatch, exception, code):
    async def handler(request):
        raise exception("timeout", request=request)
    transport(monkeypatch, handler)
    with pytest.raises(WorkerFailure) as caught:
        await backend.complete(ResponseRequest(model="claude-test", input="hi"), TARGET)
    assert caught.value.code == code and caught.value.status == 504
    assert not caught.value.safe_to_retry
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status,kind", [
    ("worker_capacity_exceeded", 503, "capacity"),
    ("worker_queue_timeout", 503, "capacity"),
    ("worker_execution_conflict", 409, "execution_conflict"),
    ("worker_connection_timeout", 504, "connection"),
])
async def test_gateway_preserves_worker_reason(monkeypatch, code, status, kind):
    from codex_gateway import main
    from codex_gateway.auth import ApiPrincipal
    failure = WorkerFailure("specific reason", kind=kind, code=code, status=status)
    class Failed:
        async def complete(self, *args):
            raise failure
        async def stream(self, *args):
            raise failure
            yield
    async def save(*args, **kwargs):
        assert args[4] == status
        assert kwargs["error_code"] == code
    monkeypatch.setattr(main, "save_usage", save)
    principal = ApiPrincipal(None, "test")
    body = ResponseRequest(model="claude-test", input="hi")
    with pytest.raises(HTTPException) as caught:
        await main.complete_with_failover(body, Failed(), principal, TARGET, allow_retry=False)
    assert caught.value.status_code == status
    assert caught.value.detail["error"]["code"] == code
    response_events = [event async for event in main.response_stream(body, Failed(), principal, TARGET, None, allow_retry=False)]
    assert code in "".join(response_events)
    chat = ChatCompletionRequest(model="claude-test", messages=[{"role": "user", "content": "hi"}])
    chat_events = [event async for event in main.chat_completion_stream(chat, body, Failed(), principal, TARGET, allow_retry=False)]
    assert code in "".join(chat_events)


@pytest.mark.parametrize("extra", [
    {"reasoning": {"effort": "minimal"}}, {"reasoning": {"effort": "high", "summary": "auto"}},
    {"text": {"format": {"type": "other"}}}, {"unknown": True},
    {"tools": [TOOL], "text": {"format": {"type": "json_object"}}},
])
def test_claude_rejects_unimplementable_options(backend, extra):
    with pytest.raises(HTTPException):
        validate_capabilities(ResponseRequest(model="claude-test", input="hi", **extra))


@pytest.mark.asyncio
async def test_text_resume_and_cache_usage(backend, monkeypatch):
    seen = []
    async def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        UUID(body["session_id"])
        rows = [{"event": "init", "thread_id": body["session_id"]}, {"delta": "hello"},
                {"done": True, "input_tokens": 30, "output_tokens": 4, "cache_read_tokens": 10, "cache_write_tokens": 7}]
        return httpx.Response(200, text="\n".join(map(json.dumps, rows)))
    transport(monkeypatch, handler)
    first = await backend.complete(ResponseRequest(model="claude-test", input="hi", instructions="Be brief"), TARGET)
    assert first.text == "hello"
    assert (first.input_tokens, first.cache_read_tokens, first.cache_write_tokens) == (30, 10, 7)
    assert seen[0]["content"] == [{"type": "text", "text": "hi"}]
    assert seen[0]["system"].startswith("Be brief")
    await backend.complete(ResponseRequest(model="claude-test", input="continue", previous_response_id=first.thread_id), TARGET)
    assert seen[1]["conversation"] == first.thread_id
    assert seen[1]["content"] == [{"type": "text", "text": "continue"}]
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["limit", "logged_out", "session", "request", "connection"])
async def test_worker_error_classification(backend, monkeypatch, kind):
    async def handler(request):
        return httpx.Response(200, text=json.dumps({"error": "failed", "kind": kind}) + "\n")
    transport(monkeypatch, handler)
    with pytest.raises(WorkerFailure) as exc:
        await backend.complete(ResponseRequest(model="claude-test", input="hi"), TARGET)
    assert exc.value.kind == kind
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reminder", [False, True])
@pytest.mark.parametrize("choice", ["auto", "required", {"type": "function", "name": "lookup"}])
async def test_tool_roundtrip_images_isolation_and_cancel(backend, monkeypatch, reminder, choice):
    state = {"reply": asyncio.Event(), "requests": []}
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (json.dumps({"event": "client_tool", "thread_id": "thread-a", "tool": "lookup",
                "run_id": "run", "worker_call_id": "private", "arguments": {"query": "hi"}}) + "\n").encode()
            await state["reply"].wait()
            yield b'{"delta":"ok","thread_id":"thread-a"}\n'
            yield b'{"done":true,"thread_id":"thread-a","input_tokens":20,"output_tokens":5,"cache_write_tokens":8}\n'
        async def aclose(self):
            state["closed"] = True
    async def handler(request):
        body = json.loads(request.content)
        state["requests"].append((request.url.path, body))
        if request.url.path == "/turn":
            return httpx.Response(200, stream=Stream())
        assert request.url.path == "/tool-result"
        state["reply"].set()
        return httpx.Response(200, json={"ok": True})
    transport(monkeypatch, handler)
    first = await backend.complete(ResponseRequest(model="claude-test", input="hi", tools=[TOOL], tool_choice=choice), TARGET)
    call = first.tool_calls[0]
    assert call["name"] == "lookup" and call["call_id"] != "private"
    followup = ResponseRequest(model="claude-test", input=[{"type": "function_call_output",
        "call_id": call["call_id"], "output": [{"type": "input_text", "text": "ok"},
        {"type": "input_image", "image_url": "data:image/png;base64," + PNG}]}])
    if reminder:
        followup.input[0]["is_error"] = True
        followup.input.append({"role": "developer", "content": [{"type": "input_text", "text": "<total_tokens>1000</total_tokens>"}]})
    with pytest.raises(ToolProtocolError):
        backend.continuation_target(followup, "other-key")
    previous_limit = backend.tool_sessions.limit
    backend.tool_sessions.limit = 0  # Admission saturation must not block results.
    final = await backend.complete(followup, TARGET)
    backend.tool_sessions.limit = previous_limit
    assert final.text == "ok" and final.cache_write_tokens == 8
    assert state["requests"][1][1]["is_error"] is reminder
    assert state["requests"][0][1]["tools"][0]["name"] == "lookup"
    image = state["requests"][1][1]["content"][1]
    assert image == {"type": "image", "mimeType": "image/png", "data": PNG}
    if reminder:
        assert "<total_tokens>1000</total_tokens>" in state["requests"][1][1]["content"][-1]["text"]
    assert state["closed"]
    with pytest.raises(ToolProtocolError):
        backend.continuation_target(followup, "key")
    first = await backend.complete(ResponseRequest(model="claude-test", input="hi", tools=[TOOL], tool_choice=choice), TARGET)
    await backend.tool_sessions.cancel_thread("key", first.thread_id)
    assert not backend.tool_sessions.pending
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("output,valid", [('{"ok":true}', True), ('{"ok":"yes"}', False)])
async def test_json_schema_is_passed_and_validated(backend, monkeypatch, output, valid):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    async def handler(request):
        assert json.loads(request.content)["json_schema"] == schema
        return httpx.Response(200, text=json.dumps({"delta": output, "thread_id": "thread"}) + '\n{"done":true}\n')
    transport(monkeypatch, handler)
    req = ResponseRequest(model="claude-test", input="hi", text={"format": {"type": "json_schema", "schema": schema}})
    if valid:
        result = await backend.complete(req, TARGET)
        assert json.loads(result.text) == {"ok": True}
    else:
        with pytest.raises(WorkerFailure, match="JSON Schema"):
            await backend.complete(req, TARGET)
    await backend.close()


@pytest.mark.asyncio
async def test_closing_stream_immediately_closes_worker_transport(backend, monkeypatch):
    closed = asyncio.Event()
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"delta":"first","thread_id":"thread"}\n'
            await asyncio.Event().wait()
        async def aclose(self):
            closed.set()
    async def handler(request):
        return httpx.Response(200, stream=Stream())
    transport(monkeypatch, handler)
    events = backend.stream(ResponseRequest(model="claude-test", input="hi"), TARGET)
    assert (await anext(events)).delta == "first"
    await events.aclose()
    assert closed.is_set()
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["required", {"type": "function", "name": "lookup"}])
async def test_forced_choice_cannot_succeed_as_text(backend, monkeypatch, choice):
    async def handler(request):
        payload = json.loads(request.content)
        assert [t["name"] for t in payload["tools"]] == ["lookup"]
        return httpx.Response(200, text='{"delta":"direct answer","thread_id":"thread"}\n{"done":true}\n')
    transport(monkeypatch, handler)
    req = ResponseRequest(model="claude-test", input="hi", tools=[TOOL], tool_choice=choice)
    assert req.unsupported() is None
    try:
        with pytest.raises(WorkerFailure) as caught:
            await backend.complete(req, TARGET)
        assert caught.value.code == "tool_choice_not_satisfied"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_configured_capacity_is_retryable(backend, monkeypatch):
    sessions = backend.claude.tool_sessions
    sessions.limit = 0
    try:
        with pytest.raises(WorkerFailure) as caught:
            await backend.complete(ResponseRequest(model="claude-test", input="hi", tools=[TOOL]), TARGET)
        assert caught.value.code == "client_tool_capacity_exceeded"
        assert caught.value.status == 429
    finally:
        await backend.close()


def test_shared_store_uses_configured_claude_limits():
    from codex_gateway.config import Settings
    backend = ProviderBackend(Settings(claude_tool_session_limit=75, claude_tool_sessions_per_key=12))
    assert backend.claude.tool_sessions is backend.tool_sessions
    assert backend.tool_sessions.limit == 75
    assert backend.tool_sessions.provider_key_limits == {"claude": 12}
    assert backend.tool_sessions.per_key_limit == 8


def test_chat_forced_choice_and_unknown_tool(backend):
    chat = ChatCompletionRequest(model="claude-test", messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {k: v for k, v in TOOL.items() if k != "type"}}],
        tool_choice={"type": "function", "function": {"name": "lookup"}})
    assert chat.unsupported() is None
    assert chat.to_response_request().tool_choice == {"type": "function", "name": "lookup"}
    chat.tool_choice["function"]["name"] = "missing"
    assert chat.unsupported() is not None
