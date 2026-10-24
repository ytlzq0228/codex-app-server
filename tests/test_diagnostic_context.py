import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from codex_gateway.diagnostics import annotate_failure, failure_diagnostic, worker_process_context
from codex_gateway.backend import BackendTarget, BackendStreamEvent
from codex_gateway.client_tools import ToolProtocolError
from codex_gateway.tool_sessions import ToolSessions
from codex_gateway.schemas import ResponseRequest


def test_exception_context_and_pool_snapshot_do_not_include_messages():
    inner = annotate_failure(ValueError("secret prompt"), "tool_capacity", key_runs=32)
    outer = RuntimeError("Bearer secret")
    outer.__cause__ = inner
    result = failure_diagnostic(outer)
    assert result["context"] == [{"category": "tool_capacity", "key_runs": 32}]
    assert "checked_out" in result["database_pool"]
    assert "secret" not in json.dumps(result)


def test_worker_diagnostic_is_an_allowlist():
    result = worker_process_context({
        "exit_code": 1, "last_event": "private prompt", "stderr_bytes": "secret",
        "stderr_categories": ["network", "secret", {"token": "secret"}],
        "stderr": "Bearer secret", "produced_output": True})
    assert result == {"exit_code": 1, "stderr_categories": ["network"], "produced_output": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["key", "global"])
async def test_capacity_evidence_describes_pending_run_without_payload(scope):
    async def events(request, target, run):
        await sessions.await_result(run, {"call_id": "call-a"})
        yield BackendStreamEvent(thread_id="thread", tool_call={"call_id": "call-a"})
        await sessions.receive_result(run)
    sessions = ToolSessions(events, per_key_limit=1 if scope == "key" else 8,
                            limit=1 if scope == "global" else 512)
    request = ResponseRequest(model="test", input="private prompt")
    target = BackendTarget("key:worker", "http://worker", "/workspace")
    try:
        _ = [event async for event in sessions.stream(request, target)]
        with pytest.raises(ToolProtocolError) as caught:
            _ = [event async for event in sessions.stream(request, target)]
        details = caught.value.gateway_diagnostic
        assert details["scope"] == scope
        assert details["key_runs"] == details["key_waiting"] == 1
        assert details["key_done"] == 0
        assert details["oldest_key_age_ms"] >= 0
        assert "private prompt" not in json.dumps(details)
    finally:
        await sessions.close()


@pytest.mark.asyncio
async def test_cancellation_diagnostic_preserves_timeout_class(monkeypatch):
    from codex_gateway import cluster
    original = httpx.AsyncClient
    async def handler(request):
        raise httpx.ConnectTimeout("private endpoint", request=request)
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    from codex_gateway.config import Settings
    with pytest.raises(HTTPException) as caught:
        await cluster.request_cancellation("http://example.com", "abandon",
            {"logical_id": "conv-test", "key_id": "test", "response_id": "resp-test"},
            Settings())
    assert caught.value.detail["error"]["code"] == "tool_owner_connect_timeout"
    context = caught.value.gateway_diagnostic
    assert context["attempts"] == 1 and context["phase"] == "submit"
    assert "example.com" not in json.dumps(context)


@pytest.mark.asyncio
async def test_http_error_handler_captures_annotated_conflict():
    from codex_gateway.main import http_error_handler
    from codex_gateway.audit import current_audit
    audit = {"request_id": "req-test"}
    token = current_audit.set(audit)
    try:
        exc = annotate_failure(HTTPException(409, detail={"error": {
            "code": "conversation_busy", "message": "busy"}}),
            "conversation_busy", lease_remaining_ms=1000)
        response = await http_error_handler(SimpleNamespace(headers={}, url=SimpleNamespace(path="/v1/responses")), exc)
        assert response.status_code == 409
        assert audit["diagnostic"]["context"][0]["lease_remaining_ms"] == 1000
        assert "diagnostic" not in json.loads(response.body)
    finally:
        current_audit.reset(token)


@pytest.mark.asyncio
async def test_conversation_wait_timeout_records_attempts(monkeypatch):
    from codex_gateway import execution
    from codex_gateway.config import get_settings
    monkeypatch.setattr(get_settings(), "claude_execution_wait_seconds", 0)
    async def busy(*args, **kwargs):
        error = annotate_failure(HTTPException(409), "conversation_busy", lease_remaining_ms=2000)
        error.execution_logical_id = "conv-test"
        raise error
    monkeypatch.setattr(execution, "_prepare", busy)
    with pytest.raises(HTTPException) as caught:
        await execution.prepare(ResponseRequest(model="claude-test", input="hi"),
                                SimpleNamespace(key_id="test"), "responses", {})
    assert caught.value.gateway_diagnostic["wait_outcome"] == "deadline"
    assert caught.value.gateway_diagnostic["attempts"] == 0


@pytest.mark.asyncio
async def test_transport_disconnect_logs_only_structure(caplog):
    from codex_gateway.audit import RequestAuditMiddleware
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"secret response", "more_body": True})
        await receive()
    async def receive():
        return {"type": "http.disconnect"}
    async def send(message):
        pass
    await RequestAuditMiddleware(app)({
        "type": "http", "method": "POST", "path": "/v1/responses",
        "headers": [], "state": {"request_id": "req-test"}}, receive, send)
    message = next(r.message for r in caplog.records if "Request transport interrupted" in r.message)
    assert '"disconnect_observed": true' in message
    assert '"body_chunks_sent": 1' in message
    assert "secret response" not in message


@pytest.mark.asyncio
async def test_remote_tool_error_retains_owner_diagnostics(monkeypatch):
    from codex_gateway import cluster
    from codex_gateway.audit import current_audit
    from codex_gateway.config import Settings
    original = httpx.AsyncClient
    async def handler(request):
        return httpx.Response(200, text=json.dumps({"error": {
            "message": "capacity", "code": "client_tool_capacity_exceeded",
            "diagnostic": {"node": "owner", "context": [
                {"category": "tool_capacity", "scope": "key", "key_runs": 32}]}}}) + "\n")
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    audit = {}
    token = current_audit.set(audit)
    try:
        with pytest.raises(ToolProtocolError):
            _ = [event async for event in cluster.remote_stream("http://example.com",
                ResponseRequest(model="test", input="hi"),
                BackendTarget("key:worker", "http://worker", "/workspace"), Settings())]
        assert audit["remote_diagnostic"]["context"][0]["key_runs"] == 32
        assert audit["remote_diagnostic"]["node"] == "owner"
    finally:
        current_audit.reset(token)
