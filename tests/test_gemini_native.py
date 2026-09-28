import json
import httpx
import pytest
from codex_gateway.gemini_native import (
    GeminiNativeMiddleware, translate_request, function_part, schema,
)
from codex_gateway.config import get_settings

def prompt():
    return {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}

@pytest.mark.parametrize("result_role", ["user", "model"])
def test_tool_result_id_survives_clients_which_omit_native_ids(result_role):
    part = function_part({"id": "call_private", "function": {"name": "read_file", "arguments": '{"path":"a"}'}})
    del part["functionCall"]["id"]
    body = prompt()
    body["contents"] += [{"role": "model", "parts": [part]},
                         {"role": result_role, "parts": [{"functionResponse": {"name": "read_file", "response": {"output": "marker"}}}]}]
    result = translate_request(body, "gemini", True)
    assert result["messages"][-1]["tool_call_id"] == "call_private"
    assert json.loads(result["messages"][-1]["content"]) == {"output": "marker"}

def test_schema_preserves_property_names_and_converts_types():
    result = schema({"type": "OBJECT", "properties": {"type": {"type": "STRING"}, "nullable": {"type": "INTEGER", "nullable": True}}})
    assert result["properties"]["type"]["type"] == "string"
    assert result["properties"]["nullable"]["anyOf"][1] == {"type": "null"}

@pytest.mark.parametrize("body", [
    {"contents": [{"parts": [{"inlineData": {"data": "x"}}]}]},
    {**prompt(), "generationConfig": {"responseMimeType": "image/png"}},
    {**prompt(), "tools": [{"googleSearch": {}}]},
    {**prompt(), "generationConfig": {"stopSequences": ["STOP"]}},
    {**prompt(), "toolConfig": {"functionCallingConfig": {"mode": "ANY"}}},
    {**prompt(), "contents": [{"role": "user", "parts": [{"functionResponse": {"name": "missing", "response": {}}}]}]},
])
def test_unsupported_input_is_explicit(body):
    with pytest.raises(ValueError):
        translate_request(body, "gemini", False)

@pytest.mark.asyncio
async def test_wire_auth_alias_stream_and_usage(monkeypatch):
    monkeypatch.setattr(get_settings(), "gemini_native_model_aliases", "flash:gemini")
    seen = {}
    async def inner(scope, receive, send):
        seen.update(scope)
        seen["body"] = json.loads((await receive())["body"])
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        chunks = [
            {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [{"id": "call_a", "function": {"name": "read_file", "arguments": '{"path":"a"}'}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16, "prompt_tokens_details": {"cached_tokens": 5}}},
        ]
        data = "".join("data: "+json.dumps(c)+"\n\n" for c in chunks).encode()+b"data: [DONE]\n\n"
        for offset in range(0, len(data), 13):
            await send({"type": "http.response.body", "body": data[offset:offset+13], "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=GeminiNativeMiddleware(inner)),base_url="http://test") as client:
        response = await client.post("/v1beta/models/flash:streamGenerateContent?alt=sse",headers={"x-goog-api-key": "test-key"},json=prompt())
    assert seen["path"] == "/v1/chat/completions"
    assert dict(seen["headers"])[b"authorization"] == b"Bearer test-key"
    assert b"x-goog-api-key" not in dict(seen["headers"])
    assert seen["body"]["model"] == "gemini"
    chunks = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert chunks[0]["candidates"][0]["content"]["parts"] == [{"text": "hello"}]
    assert chunks[1]["candidates"][0]["content"]["parts"][0]["functionCall"]["id"] == "call_a"
    assert chunks[-1]["usageMetadata"]["totalTokenCount"] == 16
    assert chunks[-1]["usageMetadata"]["cachedContentTokenCount"] == 5

@pytest.mark.asyncio
async def test_openai_passthrough_and_invalid_key_error():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"error":{"code":"invalid_api_key","message":"Invalid credentials"}}'})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=GeminiNativeMiddleware(inner)),base_url="http://test") as client:
        legacy = await client.post("/v1/chat/completions",json={})
        native = await client.post("/v1beta/models/flash:generateContent",json=prompt())
    assert legacy.json()["error"]["code"] == "invalid_api_key"
    assert native.status_code == 401
    assert native.json()["error"]["status"] == "UNAUTHENTICATED"

@pytest.mark.asyncio
async def test_request_size_limit(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_request_bytes", 10)
    async def inner(*args):
        pytest.fail("oversized request must not reach backend")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=GeminiNativeMiddleware(inner)),base_url="http://test") as client:
        response = await client.post("/v1beta/models/flash:generateContent",json=prompt())
    assert response.status_code == 413

def test_native_structured_schema_translation():
    body = {**prompt(), "generationConfig": {"responseMimeType": "application/json",
            "responseSchema": {"type": "OBJECT", "properties": {"loop": {"type": "BOOLEAN"}},
                               "required": ["loop"]}}}
    result = translate_request(body, "gemini", False)
    assert result["response_format"]["json_schema"]["schema"]["properties"]["loop"]["type"] == "boolean"

@pytest.mark.parametrize("conflicting", [False, True])
def test_resume_duplicate_function_results(conflicting):
    part = function_part({"id": "call_a", "function": {"name": "read_file", "arguments": "{}"}})
    result = {"functionResponse": {"id": "call_a", "name": "read_file", "response": {"output": "hello"}}}
    duplicate = json.loads(json.dumps(result))
    if conflicting:
        duplicate["functionResponse"]["response"]["output"] = "different"
    body = prompt()
    body["contents"] += [{"role": "model", "parts": [part]},
                         {"role": "user", "parts": [result, duplicate]},
                         {"role": "user", "parts": [{"text": "continue"}]}]
    if conflicting:
        with pytest.raises(ValueError, match="Conflicting"):
            translate_request(body, "gemini", False)
    else:
        translated = translate_request(body, "gemini", False)
        assert len([m for m in translated["messages"] if m["role"] == "tool"]) == 1
