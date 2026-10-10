import json
from uuid import uuid4

import httpx
import pytest

from codex_gateway.claude_native import ClaudeNativeMiddleware, MessageStream, native_error, native_response, translate_request
from codex_gateway.config import get_settings
from codex_gateway.conversations import explicit_identity

HEADERS = {"anthropic-version": "2023-06-01", "x-api-key": "test-key"}


def prompt():
    return {"model": "claude-test", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 10}


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(get_settings(), "allowed_models", "claude-test")
    monkeypatch.setattr(get_settings(), "model_providers", "claude-test:claude")


def result():
    return {"id": "resp_a", "output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
        {"type": "function_call", "call_id": "call_a", "name": "lookup", "arguments": '{"q":"test"}'}],
        "usage": {"input_tokens": 40, "input_tokens_details": {"cached_tokens": 12, "cache_write_tokens": 8}, "output_tokens": 5}}


def test_request_tools_images_and_history():
    body = prompt()
    body.update(system=[{"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}],
                thinking={"type": "adaptive"}, output_config={"effort": "high"}, temperature=.1,
                tools=[{"name": "lookup", "input_schema": {"type": "object"}}],
                metadata={"user_id": "test-session"})
    body["messages"] += [
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "secret", "signature": "sig"},
         {"type": "tool_use", "id": "call_a", "name": "lookup", "input": {"q": "test"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_a", "is_error": True, "content": [
         {"type": "text", "text": "ok"}, {"type": "image", "source": {"type": "url", "url": "https://example.org/a.png"}}]}]}]
    req = translate_request(body)
    assert req["instructions"] == "be brief"
    assert req["reasoning"] == {"effort": "high"}
    assert "max_tokens" not in req and "temperature" not in req and "thinking" not in req
    assert req["input"][-1]["call_id"] == "call_a"
    assert req["input"][-1]["is_error"] is True
    assert req["input"][-1]["output"][1]["type"] == "input_image"
    assert req["input"][-2]["arguments"] == '{"q": "test"}'
    assert req["metadata"]["user_id"] == "test-session"


@pytest.mark.parametrize("extra", [
    {"messages": [{"role": "assistant", "content": "prefix"}]},
    {"tool_choice": {"type": "any"}}, {"tool_choice": {"type": "tool", "name": "x"}},
    {"tools": [{"type": "bash_20250124", "name": "bash"}]}, {"mcp_servers": []},
    {"messages": [{"role": "user", "content": [{"type": "document"}]}]},
    {"system": [{"type": "image"}]}, {"stream": "yes"},
])
def test_structural_rejections(extra):
    with pytest.raises((ValueError, KeyError)):
        translate_request(prompt() | extra)


def test_cache_usage_and_tool_ids():
    obj = native_response(result(), "claude-test")
    assert obj["content"][1]["id"] == "call_a"
    assert obj["stop_reason"] == "tool_use"
    assert obj["usage"] == {"input_tokens": 20, "output_tokens": 5,
        "cache_read_input_tokens": 12, "cache_creation_input_tokens": 8}


@pytest.mark.parametrize("status,code,expected,kind", [
    (401, "", 401, "authentication_error"), (403, "", 403, "permission_error"),
    (404, "", 404, "not_found_error"), (422, "", 400, "invalid_request_error"),
    (502, "", 500, "api_error"), (503, "worker_capacity_exceeded", 529, "overloaded_error"),
    (502, "provider_quota_exhausted", 429, "rate_limit_error"),
    (503, "worker_queue_timeout", 529, "overloaded_error"),
    (502, "worker_execution_conflict", 409, "api_error"),
    (502, "worker_connection_timeout", 504, "api_error"),
    (502, "worker_transport_timeout", 504, "api_error"),
])
def test_error_mapping(status, code, expected, kind):
    actual, obj = native_error({"error": {"message": "failed", "code": code}}, status)
    assert actual == expected and obj["error"]["type"] == kind


@pytest.mark.asyncio
async def test_wire_alias_auth_fragmented_sse(monkeypatch):
    monkeypatch.setattr(get_settings(), "claude_native_model_aliases", "alias:claude-test")
    seen = {}
    async def inner(scope, receive, send):
        seen.update(scope)
        seen["body"] = json.loads((await receive())["body"])
        await send({"type": "http.response.start", "status": 200, "headers": [(b"x-request-id", b"req_a")]})
        events = [{"type": "response.created", "response": {"id": "resp_a"}},
                  {"type": "response.output_text.delta", "delta": "hello"},
                  {"type": "response.output_text.done"},
                  {"type": "response.output_item.done", "item": result()["output"][1]},
                  {"type": "response.completed", "response": result()}]
        wire = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()
        for i in range(0, len(wire), 7):
            await send({"type": "http.response.body", "body": wire[i:i+7], "more_body": True})
        await send({"type": "http.response.body", "body": b""})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ClaudeNativeMiddleware(inner)), base_url="http://test") as client:
        response = await client.post("/v1/messages?beta=true", headers=HEADERS, json=prompt() | {"model": "alias", "stream": True})
    assert seen["path"] == "/v1/responses"
    assert dict(seen["headers"])[b"authorization"] == b"Bearer test-key"
    assert b"x-api-key" not in dict(seen["headers"])
    assert seen["body"]["model"] == "claude-test"
    assert response.headers["request-id"] == "req_a"
    assert response.headers["x-gateway-generation-policy"] == "worker-defaults"
    events = [json.loads(l[6:]) for l in response.text.splitlines() if l.startswith("data: ")]
    assert [e["type"] for e in events] == ["message_start", "content_block_start", "content_block_delta",
        "content_block_stop", "content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop"]
    assert events[4]["content_block"]["id"] == "call_a"
    assert events[-2]["usage"]["input_tokens"] == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status,expected", [("worker_capacity_exceeded", 503, 529), ("provider_quota_exhausted", 429, 429)])
async def test_http_error_status_is_translated_before_headers(code, status, expected):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": json.dumps({"error": {"code": code, "message": "failed"}}).encode()})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ClaudeNativeMiddleware(inner)), base_url="http://test") as client:
        response = await client.post("/v1/messages", headers=HEADERS, json=prompt())
    assert response.status_code == expected
    assert response.json()["type"] == "error"


@pytest.mark.asyncio
async def test_missing_version_oversize_model_and_passthrough(monkeypatch):
    seen = []
    async def inner(scope, receive, send):
        seen.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ClaudeNativeMiddleware(inner)), base_url="http://test") as client:
        assert (await client.post("/v1/messages", json=prompt())).status_code == 400
        assert (await client.post("/v1/messages", headers=HEADERS, json=prompt() | {"model": "other"})).status_code == 404
        assert (await client.post("/v1/responses", json={})).status_code == 200
        monkeypatch.setattr(get_settings(), "max_request_bytes", 10)
        assert (await client.post("/v1/messages", headers=HEADERS, json=prompt())).status_code == 413
    assert seen == ["/v1/responses"]


def test_session_identity_is_key_scoped():
    uid = str(uuid4())
    params = {"model": "claude-test", "metadata": {"user_id": "user_hash_account_a_session_" + uid}}
    a, evidence = explicit_identity(params, {}, "key-a", "responses", "a")
    b, _ = explicit_identity(params, {}, "key-a", "responses", "b")
    c, _ = explicit_identity(params, {}, "key-b", "responses", "c")
    assert a == b and a != c
    assert evidence["client_thread_ids"] == [uid]


def test_stream_failure_is_not_followed_by_success():
    state = MessageStream("claude-test")
    event = {"type": "error", "code": "provider_quota_exhausted", "message": "limit"}
    assert state.translate(event)[0]["error"]["type"] == "rate_limit_error"
    assert state.translate({"type": "response.failed", "response": {"error": event}}) == []
    assert state.translate({"type": "response.completed", "response": result()}) == []


def test_agent_identity_uses_headers_not_prompt():
    uid = str(uuid4())
    params = translate_request(prompt() | {'metadata': {'user_id': json.dumps({'session_id': uid.upper()})}})
    def identify(agent=None, session=uid, key='k', endpoint='responses', body=params):
        headers = [{'name': 'x-claude-code-session-id', 'value': session}]
        if agent is not None:
            headers.append({'name': 'x-claude-code-agent-id', 'value': agent})
        return explicit_identity(body, {'headers': headers}, key, endpoint, '')
    main, evidence = identify()
    assert evidence['client_thread_ids'] == [uid]
    branches = [identify(f'agent-{i}')[0] for i in range(4)]
    assert len(set([main, *branches, identify('main')[0]])) == 6
    assert identify('agent-0', body=params | {'input': [{'role': 'user', 'content': 'compacted'}],
                                             'instructions': 'changed', 'tools': []})[0] == branches[0]
    assert identify('agent-0', key='other')[0] != branches[0]
    assert identify('agent-0', endpoint='chat.completions')[0] != branches[0]
    assert identify('agent-0', session=str(uuid4()))[1]['method'] == 'identifier_conflict'
    headers = {'headers': [{'name': 'x-claude-code-agent-id', 'value': a} for a in ['a', 'b']]}
    assert explicit_identity(params, headers, 'k', 'responses', '')[0] is None
    helper = params | {'text': {'format': {'type': 'json_schema', 'schema': {'type': 'object'}}}}
    assert identify(body=helper)[1]['category'] == 'claude_auxiliary'
    assert identify('agent-0', body=helper)[0] == branches[0]


@pytest.mark.parametrize('user_id', ['plain-sdk-id', '{invalid-json', '{"session_id":123}', '{"session_id":"invalid"}'])
def test_sdk_user_id_fallback(user_id):
    _, evidence = explicit_identity({'model': 'claude-test', 'metadata': {'user_id': user_id}}, {}, 'k', 'responses', '')
    assert evidence['client_thread_ids'] == [user_id]


def test_bare_client_empty_system_reminder_is_a_noop():
    body = prompt()
    expected = translate_request(body)
    body["messages"].append({"role": "system", "content": []})
    assert translate_request(body) == expected
    body["messages"][-1]["role"] = "user"
    with pytest.raises(ValueError):
        translate_request(body)


@pytest.mark.parametrize("choice,expected", [
    ({"type": "any"}, "required"),
    ({"type": "tool", "name": "lookup"}, {"type": "function", "name": "lookup"}),
])
def test_forced_choice_translation(choice, expected):
    body = prompt() | {"tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
                       "tool_choice": choice}
    assert translate_request(body)["tool_choice"] == expected
