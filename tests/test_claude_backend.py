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
async def test_tool_roundtrip_images_isolation_and_cancel(backend, monkeypatch, reminder):
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
    first = await backend.complete(ResponseRequest(model="claude-test", input="hi", tools=[TOOL]), TARGET)
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
    final = await backend.complete(followup, TARGET)
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
    first = await backend.complete(ResponseRequest(model="claude-test", input="hi", tools=[TOOL]), TARGET)
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
