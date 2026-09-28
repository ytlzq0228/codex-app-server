import asyncio
import json
import httpx
import pytest

from codex_gateway.backend import BackendTarget, WorkerFailure
from codex_gateway.client_tools import ToolProtocolError
from codex_gateway.config import get_settings
from codex_gateway.gemini_backend import ProviderBackend
from codex_gateway.providers import validate_capabilities
from codex_gateway.schemas import ResponseRequest, ChatCompletionRequest

TOOL = {"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}


@pytest.fixture
def gemini_backend(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "model_providers", "gemini-test:gemini")
    backend = ProviderBackend(settings)
    assert backend.tool_sessions is backend.gemini.tool_sessions
    return backend


class WorkerStream(httpx.AsyncByteStream):
    def __init__(self, state):
        self.state = state

    async def __aiter__(self):
        yield json.dumps({"heartbeat": True}).encode() + b"\n"
        yield json.dumps({"event": "client_tool", "run_id": "private-run", "worker_call_id": "private-call",
                          "thread_id": "thread-a", "tool": self.state.get("tool", "gateway_client_0"),
                          "arguments": self.state.get("arguments", {"query": "dns"}),
                          "input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 4}).encode() + b"\n"
        await self.state["replied"].wait()
        if "correction" in self.state:
            self.state["replied"].clear()
            yield json.dumps({"event": "client_tool", "run_id": "private-run", "worker_call_id": "corrected-call",
                              "thread_id": "thread-a", "tool": "gateway_client_0",
                              "arguments": self.state["correction"], "input_tokens": 12,
                              "output_tokens": 3, "cache_read_tokens": 5}).encode() + b"\n"
            await self.state["replied"].wait()
        yield json.dumps({"thread_id": "thread-a", "delta": "client result received"}).encode() + b"\n"
        yield json.dumps({"thread_id": "thread-a", "done": True, "input_tokens": 16,
                          "output_tokens": 5, "cache_read_tokens": 6}).encode() + b"\n"

    async def aclose(self):
        self.state["closed"] = True


def mock_worker(monkeypatch, **overrides):
    state = {"replied": asyncio.Event(), "requests": [], **overrides}
    async def handle(request):
        payload = json.loads(request.content)
        state["requests"].append((request.url.path, payload))
        if request.url.path == "/capabilities":
            return httpx.Response(200, json={"client_tools": 1})
        if request.url.path == "/turn":
            return httpx.Response(200, stream=WorkerStream(state))
        assert request.url.path == "/tool-result"
        state["replied"].set()
        return httpx.Response(200, json={"ok": True})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_gemini_tool_roundtrip_and_usage(gemini_backend, monkeypatch, streaming):
    state = mock_worker(monkeypatch)
    backend = gemini_backend
    target = BackendTarget("key:worker", "http://worker", "/workspace/key", provider="gemini")
    request = ResponseRequest(model="gemini-test", input="look up dns", tools=[TOOL])
    validate_capabilities(request)
    try:
        if streaming:
            events = [event async for event in backend.stream(request, target)]
            call = events[-1].tool_call
        else:
            first = await backend.complete(request, target)
            assert first.input_tokens == 10 and first.cache_read_tokens == 4
            call = first.tool_calls[0]
        assert call["name"] == "lookup"
        assert json.loads(call["arguments"]) == {"query": "dns"}
        assert call["call_id"] != "private-call"
        followup = ResponseRequest(model="gemini-test", input=[
            {"type": "function_call_output", "call_id": call["call_id"], "output": "client answer"}])
        assert backend.continuation_target(followup, "key") is target
        assert backend.continuation_thread(followup, "key") == "thread-a"
        with pytest.raises(ToolProtocolError):
            backend.continuation_target(followup, "another-key")
        final = await backend.complete(followup, target)
        assert final.text == "client result received"
        assert (final.input_tokens, final.output_tokens, final.cache_read_tokens) == (6, 3, 2)
        assert len([path for path, _ in state["requests"] if path == "/turn"]) == 1
        assert state["requests"][1][1]["tools"][0]["name"] == "gateway_client_0"
        assert state["requests"][2][1]["content"] == [{"type": "text", "text": "client answer"}]
        with pytest.raises(ToolProtocolError):
            backend.continuation_target(followup, "key")
    finally:
        await backend.close()
    assert state["closed"]


@pytest.mark.asyncio
async def test_chat_custom_namespace_and_cancel(gemini_backend, monkeypatch):
    state = mock_worker(monkeypatch, arguments={"input": "hello"})
    target = BackendTarget("key:worker", "http://worker", "/workspace/key", provider="gemini")
    request = ResponseRequest(model="gemini-test", input="hello", tools=[
        {"type": "namespace", "name": "functions", "tools": [{"type": "custom", "name": "exec"}]}])
    result = await gemini_backend.complete(request, target)
    assert result.tool_calls[0]["type"] == "custom_tool_call"
    assert result.tool_calls[0]["namespace"] == "functions"
    assert result.tool_calls[0]["input"] == "hello"
    await gemini_backend.tool_sessions.cancel_thread("key", "thread-a")
    assert state["closed"]
    assert not gemini_backend.tool_sessions.pending
    await gemini_backend.close()


@pytest.mark.asyncio
async def test_undeclared_tool_fails_closed(gemini_backend, monkeypatch):
    state = mock_worker(monkeypatch, tool="run_command")
    try:
        with pytest.raises(WorkerFailure):
            await gemini_backend.complete(ResponseRequest(model="gemini-test", input="x", tools=[TOOL]),
                                          BackendTarget("key:worker", "http://worker", "/workspace/key", provider="gemini"))
        assert len(state["requests"]) == 2
    finally:
        await gemini_backend.close()


def test_chat_tool_conversion_and_image_results(gemini_backend):
    from fastapi import HTTPException
    body = ChatCompletionRequest(model="gemini-test", messages=[{"role": "user", "content": "hi"}],
                                 tools=[{"type": "function", "function": {k: v for k, v in TOOL.items() if k != "type"}}])
    validate_capabilities(body.to_response_request())
    validate_capabilities(ResponseRequest(model="gemini-test", input=[{
        "type": "function_call_output", "call_id": "call-x", "output": [{"type": "text", "text": "ok"}]}]))
    with pytest.raises(HTTPException):
        validate_capabilities(ResponseRequest(model="gemini-test", input=[{
            "type": "function_call_output", "call_id": "call-x", "output": [{"type": "input_image", "image_url": "https://example.org/image.png"}]}]))

@pytest.mark.asyncio
async def test_custom_grammar_stream(gemini_backend, monkeypatch):
    state = mock_worker(monkeypatch, arguments={"input": "weather"})
    target = BackendTarget("key:worker", "http://worker", "/workspace/key", provider="gemini")
    request = ResponseRequest(model="gemini-test", input="lookup", tools=[{
        "type": "namespace", "name": "functions", "tools": [{
            "type": "custom", "name": "lookup",
            "format": {"type": "grammar", "syntax": "lark", "definition": 'start: "weather"'}}]}])
    try:
        events = [event async for event in gemini_backend.stream(request, target)]
        assert events[-1].tool_call["input"] == "weather"
        followup = ResponseRequest(model="gemini-test", input=[{
            "type": "custom_tool_call_output", "call_id": events[-1].tool_call["call_id"], "output": "ok"}])
        final = [event async for event in gemini_backend.stream(followup, target)]
        assert final[-1].done
    finally:
        await gemini_backend.close()


@pytest.mark.asyncio
async def test_old_worker_rejected_before_execution(gemini_backend, monkeypatch):
    paths = []
    async def handle(request):
        paths.append(request.url.path)
        return httpx.Response(404)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    try:
        with pytest.raises(WorkerFailure, match="upgraded"):
            await gemini_backend.complete(ResponseRequest(model="gemini-test", input="x", tools=[TOOL]),
                BackendTarget("key:worker", "http://worker", "/workspace/key", provider="gemini"))
        assert paths == ["/capabilities"]
    finally:
        await gemini_backend.close()


def test_sequential_tools_option(gemini_backend):
    from codex_gateway.providers import validate_chat_capabilities
    request = ResponseRequest(model="gemini-test", input="x", tools=[TOOL])
    validate_capabilities(request)
    assert request.parallel_tool_calls is False
    validate_capabilities(ResponseRequest(model="gemini-test", input="x", tools=[TOOL], parallel_tool_calls=False))
    validate_chat_capabilities(ChatCompletionRequest(model="gemini-test", messages=[{"role": "user", "content": "x"}],
                                                    parallel_tool_calls=False))


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"query": "weather"}, {"input": "invalid"}])
async def test_invalid_custom_input_is_corrected_before_delivery(gemini_backend, monkeypatch, arguments):
    state = mock_worker(monkeypatch, arguments=arguments, correction={"input": "weather"})
    target = BackendTarget("key:worker", "http://worker", "/workspace/key", provider="gemini")
    request = ResponseRequest(model="gemini-test", input="lookup", tools=[{
        "type": "custom", "name": "lookup",
        "format": {"type": "grammar", "syntax": "lark", "definition": 'start: "weather"'}}])
    try:
        first = await gemini_backend.complete(request, target)
        assert len(first.tool_calls) == 1 and first.tool_calls[0]["input"] == "weather"
        correction = state["requests"][2][1]
        assert correction["is_error"] is True
        followup = ResponseRequest(model="gemini-test", input=[{
            "type": "custom_tool_call_output", "call_id": first.tool_calls[0]["call_id"], "output": "ok"}])
        final = await gemini_backend.complete(followup, target)
        assert final.input_tokens == 4 and final.cache_read_tokens == 1
    finally:
        await gemini_backend.close()


@pytest.mark.asyncio
async def test_invalid_custom_input_without_correction_fails(gemini_backend, monkeypatch):
    mock_worker(monkeypatch, arguments={"query": "wrong"})
    request = ResponseRequest(model="gemini-test", input="lookup", tools=[{"type": "custom", "name": "lookup"}])
    try:
        with pytest.raises(WorkerFailure, match="without correcting"):
            await gemini_backend.complete(request, BackendTarget("key:worker", "http://worker", "/workspace/key", provider="gemini"))
    finally:
        await gemini_backend.close()

@pytest.mark.asyncio
async def test_gemini_rejects_missing_required_arguments_before_client_delivery(gemini_backend, monkeypatch):
    state = mock_worker(monkeypatch, arguments={}, correction={"query": "fixed"})
    tool = {**TOOL, "parameters": {**TOOL["parameters"], "required": ["query"]}}
    request = ResponseRequest(model="gemini-test", input="lookup", tools=[tool])
    target = BackendTarget("key:worker", "http://worker", "/workspace/key", provider="gemini")
    try:
        result = await gemini_backend.complete(request, target)
        assert json.loads(result.tool_calls[0]["arguments"]) == {"query": "fixed"}
        replies = [body for path, body in state["requests"] if path == "/tool-result"]
        assert len(replies) == 1 and replies[0]["is_error"]
        assert "required" in replies[0]["content"][0]["text"]
    finally:
        await gemini_backend.tool_sessions.close()
