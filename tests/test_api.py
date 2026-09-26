from fastapi.testclient import TestClient
from codex_gateway.main import app
from codex_gateway.schemas import ChatCompletionRequest

AUTH = {"Authorization": "Bearer cag_dev_local"}

def test_health() -> None:
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}

def test_auth_is_required() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/models")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"
        assert response.json()["error"]["param"] is None


def test_models_include_direct_codex_models() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/models", headers=AUTH)
        assert response.status_code == 200
        assert "gpt-5.6-sol" in {model["id"] for model in response.json()["data"]}

        detail = client.get("/v1/models/gpt-5.6-sol", headers=AUTH)
        assert detail.status_code == 200
        assert detail.json()["object"] == "model"

def test_response() -> None:
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=AUTH, json={"model": "gpt-6-sol", "input": "hello world"})
        payload = response.json()
        assert response.status_code == 200
        assert payload["object"] == "response"
        assert payload["output"][0]["content"][0]["text"] == "mock: hello world"
        assert payload["usage"]["total_tokens"] == 5

def test_streaming_response() -> None:
    with TestClient(app) as client:
        with client.stream("POST", "/v1/responses", headers=AUTH, json={"model": "gpt-6-sol", "input": "hello", "stream": True}) as response:
            body = "".join(response.iter_text())
        assert response.status_code == 200
        assert "event: response.created" in body
        assert "event: response.in_progress" in body
        assert "event: response.output_text.delta" in body
        assert "event: response.content_part.done" in body
        assert "event: response.completed" in body


def test_chat_completion() -> None:
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", headers=AUTH, json={"model": "gpt-6-sol", "messages": [{"role": "system", "content": "Be terse"}, {"role": "user", "content": "hello"}]})
        payload = response.json()
        assert response.status_code == 200
        assert payload["object"] == "chat.completion"
        assert payload["id"].startswith("chatcmpl-")
        assert payload["choices"][0]["message"]["role"] == "assistant"
        assert "USER:\nhello" in payload["choices"][0]["message"]["content"]
        assert payload["choices"][0]["finish_reason"] == "stop"


def test_chat_completion_accepts_direct_model_name() -> None:
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", headers=AUTH, json={"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hello"}]})
        assert response.status_code == 200
        assert response.json()["model"] == "gpt-5.6-sol"


def test_streaming_chat_completion() -> None:
    with TestClient(app) as client:
        with client.stream("POST", "/v1/chat/completions", headers=AUTH, json={"model": "gpt-6-sol", "messages": [{"role": "user", "content": "hello"}], "stream": True, "stream_options": {"include_usage": True}}) as response:
            body = "".join(response.iter_text())
        assert response.status_code == 200
        assert '"object":"chat.completion.chunk"' in body
        assert '"role":"assistant"' in body
        assert '"finish_reason":"stop"' in body
        assert '"usage":{"prompt_tokens"' in body
        assert body.rstrip().endswith("data: [DONE]")


def test_chatbox_style_official_options_are_accepted() -> None:
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", headers=AUTH, json={
            "model": "gpt-6-sol",
            "messages": [{"role": "developer", "content": "Be concise"}, {"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            "temperature": 0.7,
            "top_p": 1,
            "frequency_penalty": 0,
            "presence_penalty": 0,
            "reasoning_effort": "medium",
            "verbosity": "medium",
            "service_tier": "auto",
            "prompt_cache_key": "chatbox-session",
            "stream_options": {"include_usage": True, "include_obfuscation": False},
            "future_sdk_field": "accepted",
        })
        assert response.status_code == 200
        assert response.json()["service_tier"] == "auto"
        assert response.headers["x-request-id"].startswith("req_")


def test_advertised_tools_are_accepted_but_forced_tool_choice_is_rejected() -> None:
    with TestClient(app) as client:
        payload = {
            "model": "gpt-6-sol", "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}],
        }
        response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
        assert response.status_code == 200

        payload["tool_choice"] = {"type": "function", "function": {"name": "weather"}}
        response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unsupported_parameter"
        assert response.json()["error"]["param"] == "tool_choice"


def test_validation_error_identifies_parameter() -> None:
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", headers=AUTH, json={"model": "gpt-6-sol", "messages": [], "n": 0})
        assert response.status_code == 400
        assert response.json()["error"]["param"] == "n"


def test_response_echoes_official_configuration_fields() -> None:
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=AUTH, json={
            "model": "gpt-6-sol", "input": "hello", "instructions": "Be concise",
            "metadata": {"job": "test"}, "max_output_tokens": 100,
            "reasoning": {"effort": "medium"}, "truncation": "auto", "store": False,
            "future_sdk_field": True,
        })
        assert response.status_code == 200
        payload = response.json()
        assert payload["instructions"] == "Be concise"
        assert payload["metadata"] == {"job": "test"}
        assert payload["max_output_tokens"] == 100
        assert payload["reasoning"] == {"effort": "medium"}
        assert payload["truncation"] == "auto"
        assert payload["store"] is False


def test_responses_accepts_native_input_item_variants() -> None:
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=AUTH, json={
            "model": "gpt-6-sol",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Find the result"}]},
                {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{\"q\":\"x\"}"},
                {"type": "function_call_output", "call_id": "call_1", "output": "result x"},
                "continue",
            ],
        })
        assert response.status_code == 200
        text = response.json()["output"][0]["content"][0]["text"]
        assert "USER:\nFind the result" in text
        assert "TOOL OUTPUT:\nresult x" in text


def test_structured_output_and_reasoning_are_mapped_to_backend_request() -> None:
    chat = ChatCompletionRequest.model_validate({
        "model": "gpt-6-sol",
        "messages": [{"role": "user", "content": "Return an object"}],
        "reasoning_effort": "high",
        "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}}},
    })
    request = chat.to_response_request()
    assert request.reasoning == {"effort": "high"}
    assert request.output_schema() == {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


def test_removed_codex_alias_is_not_available() -> None:
    with TestClient(app) as client:
        models = client.get('/v1/models', headers=AUTH).json()['data']
        assert 'codex' not in {model['id'] for model in models}
        assert client.get('/v1/models/codex', headers=AUTH).status_code == 404
        for path, body in [('/v1/responses', {'input':'hello'}),
                           ('/v1/chat/completions', {'messages':[{'role':'user','content':'hello'}]})]:
            response = client.post(path, headers=AUTH, json={'model':'codex', **body})
            assert response.status_code == 400
            assert response.json()['error']['code'] == 'model_not_found'
