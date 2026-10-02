"""Gateway integration tests: native authentication, accounting and public headers."""
import json

from fastapi.testclient import TestClient
import pytest

from codex_gateway import main
from codex_gateway.backend import BackendTarget
from codex_gateway.config import get_settings
from codex_gateway.schemas import BackendResult, BackendStreamEvent


@pytest.fixture
def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "allowed_models", settings.allowed_models + ",claude-test")
    monkeypatch.setattr(settings, "model_providers", "claude-test:claude")
    class Backend:
        async def complete(self, request, target):
            return BackendResult(text="hello", thread_id="thread-test", input_tokens=20, output_tokens=3,
                                 cache_read_tokens=5, cache_write_tokens=4)
        async def stream(self, request, target):
            yield BackendStreamEvent(delta="hello", thread_id="thread-test")
            yield BackendStreamEvent(done=True, thread_id="thread-test", input_tokens=20, output_tokens=3,
                                     cache_read_tokens=5, cache_write_tokens=4)
        async def close(self):
            pass
    async def target(*args, **kwargs):
        return BackendTarget("development:test", "http://worker", "/workspace", provider="claude")
    monkeypatch.setattr(main, "choose_target", target)
    with TestClient(main.app) as client:
        main.app.state.backend = Backend()
        yield client


@pytest.mark.parametrize("stream", [False, True])
def test_native_pipeline(client, stream):
    response = client.post("/v1/messages?beta=true", headers={
        "x-api-key": "cag_dev_local", "anthropic-version": "2023-06-01", "anthropic-beta": "test-beta"},
        json={"model": "claude-test", "max_tokens": 1, "stream": stream,
              "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200, response.text
    assert response.headers["x-gateway-generation-policy"] == "worker-defaults"
    assert response.headers["request-id"] == response.headers["x-request-id"]
    if stream:
        events = [json.loads(l[6:]) for l in response.text.splitlines() if l.startswith("data: ")]
        assert events[0]["type"] == "message_start"
        assert events[-1]["type"] == "message_stop"
        assert events[-2]["usage"]["input_tokens"] == 11
    else:
        assert response.json()["content"] == [{"type": "text", "text": "hello"}]
        assert response.json()["usage"]["input_tokens"] == 11


def test_native_and_count_require_auth(client):
    body = {"model": "claude-test", "messages": [{"role": "user", "content": "hi"}]}
    for path in ("/v1/messages", "/v1/messages/count_tokens"):
        response = client.post(path, headers={"anthropic-version": "2023-06-01"}, json=body)
        assert response.status_code == 401 and response.json()["error"]["type"] == "authentication_error"
    response = client.post("/v1/messages/count_tokens", headers={
        "x-api-key": "cag_dev_local", "anthropic-version": "2023-06-01"}, json=body)
    assert response.status_code == 200
    assert response.json()["input_tokens"] > 0
    assert response.headers["x-gateway-token-count"] == "estimated"


@pytest.mark.parametrize("path,body", [
    ("/v1/responses", {"input": "hi", "temperature": 0, "max_output_tokens": 1}),
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1, "stop": ["END"]}),
])
@pytest.mark.parametrize("stream", [False, True])
def test_openai_policy_header(client, path, body, stream):
    response = client.post(path, headers={"Authorization": "Bearer cag_dev_local"},
                           json={"model": "claude-test", "stream": stream, **body})
    assert response.status_code == 200, response.text
    assert response.headers["x-gateway-generation-policy"] == "worker-defaults"


@pytest.mark.parametrize("kind,status,error_type", [
    ("limit", 429, "rate_limit_error"), ("capacity", 529, "overloaded_error"),
    ("request", 400, "invalid_request_error"), ("session", 404, "not_found_error"),
    ("connection", 500, "api_error"),
])
@pytest.mark.parametrize("stream", [False, True])
def test_native_backend_failure_mapping(client, kind, status, error_type, stream):
    from codex_gateway.backend import WorkerFailure
    class Failure:
        async def complete(self, *args):
            raise WorkerFailure("Test failure", kind=kind)
        async def stream(self, *args):
            raise WorkerFailure("Test failure", kind=kind)
            yield
        async def close(self):
            pass
    main.app.state.backend = Failure()
    response = client.post("/v1/messages", headers={
        "x-api-key": "cag_dev_local", "anthropic-version": "2023-06-01"},
        json={"model": "claude-test", "stream": stream, "messages": [{"role": "user", "content": "hi"}]})
    if stream:
        assert response.status_code == 200
        events = [json.loads(l[6:]) for l in response.text.splitlines() if l.startswith("data: ")]
        assert events[-1]["type"] == "error"
        assert events[-1]["error"]["type"] == error_type
        assert not any(e["type"] == "message_stop" for e in events)
    else:
        assert response.status_code == status, response.text
        assert response.json()["error"]["type"] == error_type


def test_native_size_guard_keeps_native_error_and_request_id(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_request_bytes", 10)
    response = client.post("/v1/messages", headers={"anthropic-version": "2023-06-01"},
                           json={"model": "claude-test", "messages": [{"role": "user", "content": "large"}]})
    assert response.status_code == 413
    assert response.json()["type"] == "error"
    assert response.json()["error"]["type"] == "request_too_large"
    assert response.headers["request-id"] == response.headers["x-request-id"]
