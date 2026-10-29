"""Detached recovery never claims an old RPC or repeats a side-effecting tool."""
import asyncio
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from codex_gateway import execution as ex, cluster
from codex_gateway.client_tools import ToolProtocolError, tool_outputs
from codex_gateway.config import get_settings
from codex_gateway.schemas import ResponseRequest, ChatCompletionRequest


def request_and_audit(kind="function"):
    call = {"type": kind + "_call", "call_id": "call-lost", "name": "charge_card"}
    call["arguments" if kind == "function" else "input"] = '{"amount":10}'
    request = ResponseRequest(model="gpt-6-sol", tools=[{"type": "custom" if kind == "custom_tool" else kind, "name": "charge_card"}],
                              tool_choice="required", input=[
        {"role": "user", "content": "Pay once and report the result"},
        call,
        {"type": kind + "_call_output", "call_id": "call-lost", "output": "Payment succeeded"},
        {"role": "developer", "content": "Use concise answers"},
        {"type": "additional_tools", "tools": [{"type": "function", "name": "extra"}]},
    ])
    # Additional tools normally precede the conversation.
    request.input.insert(0, request.input.pop())
    audit = {"body": json.dumps(request.model_dump(mode="json")).encode(),
             "body_complete": True, "body_bytes_received": 1, "body_hash": hashlib.sha256(),
             "transport": {"headers": []}}
    return request, audit, SimpleNamespace(key_id=uuid4())


@pytest.mark.parametrize("kind", ["function", "custom_tool"])
@pytest.mark.parametrize("code", ["client_tool_call_unavailable", "client_tool_call_expired", "client_tool_call_cancelled"])
def test_lost_result_becomes_separate_no_tools_turn(monkeypatch, kind, code):
    monkeypatch.setattr(get_settings(), "execution_resume_enabled", True)
    request, audit, principal = request_and_audit(kind)
    original = request.model_dump()
    recovered = ex.recover_tool_context(request, principal, audit, code)
    assert recovered is not None
    assert recovered.previous_response_id is None
    assert recovered.tools == [] and recovered.tool_choice == "none"
    assert not tool_outputs(recovered)
    assert ex.RECOVERY_NOTICE in recovered.instructions
    assert all(i.get("type") != "additional_tools" for i in recovered.input)
    assert recovered.input[-3]["output"] == "Payment succeeded"
    assert request.model_dump() == original

    def no_database():
        pytest.fail("Detached recovery must not read or overwrite an active conversation")
    monkeypatch.setattr(ex, "SessionLocal", no_database)
    prepared, binding = asyncio.run(ex.prepare(recovered, principal, "responses", audit))
    assert binding is None and prepared is recovered
    assert "execution" not in audit
    assert audit["execution_decision"]["trigger"] == code


@pytest.mark.parametrize("invalid", ["partial", "foreign", "duplicate", "reversed", "kind",
                                    "previous", "truncated", "incomplete", "notes", "disabled",
                                    "unauthenticated", "duplicate_error", "configuration_error"])
def test_ambiguous_or_explicit_continuations_keep_validation(monkeypatch, invalid):
    monkeypatch.setattr(get_settings(), "execution_resume_enabled", True)
    request, audit, principal = request_and_audit()
    code = "client_tool_call_unavailable"
    if invalid == "partial": request.input = request.input[2:]
    if invalid == "foreign": request.input[2]["call_id"] = "another"
    if invalid == "duplicate": request.input.insert(2, dict(request.input[2]))
    if invalid == "reversed": request.input[2], request.input[3] = request.input[3], request.input[2]
    if invalid == "kind": request.input[2]["type"] = "custom_tool_call"
    if invalid == "previous": request.previous_response_id = "resp-owned-elsewhere"
    if invalid == "truncated": audit["truncated"] = True
    if invalid == "incomplete": audit["body_complete"] = False
    if invalid == "notes": audit["transport"]["capture_notes"] = ["incomplete"]
    if invalid == "disabled": monkeypatch.setattr(get_settings(), "execution_resume_enabled", False)
    if invalid == "unauthenticated": principal.key_id = None
    if invalid == "duplicate_error": code = "client_tool_result_duplicate"
    if invalid == "configuration_error": code = "tool_configuration_changed"
    assert ex.recover_tool_context(request, principal, audit, code) is None


def test_chat_without_identity_recovers_and_live_rpc_wins(monkeypatch):
    monkeypatch.setattr(get_settings(), "execution_resume_enabled", True)
    chat = ChatCompletionRequest(model="gpt-6-sol", messages=[
        {"role": "user", "content": "Run the task"},
        {"role": "assistant", "tool_calls": [{"id": "call-lost", "type": "function",
            "function": {"name": "task", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-lost", "content": "Task complete"},
    ])
    request = chat.to_response_request()
    audit = {"body": json.dumps(chat.model_dump(mode="json")).encode(),
             "body_complete": True, "body_bytes_received": 1, "body_hash": hashlib.sha256(),
             "transport": {"headers": []}}
    principal = SimpleNamespace(key_id=uuid4())
    async def no_checkpoint(*args, **kwargs): return None
    async def lost(*args): raise ToolProtocolError("gone", "client_tool_call_unavailable")
    monkeypatch.setattr(ex, "recover_lost_output", no_checkpoint)
    monkeypatch.setattr(cluster, "pending_route", lost)
    recovered, target, thread = asyncio.run(cluster.recoverable_pending_route(
        request, principal, "chat.completions", None, audit))
    assert recovered._detached_tool_recovery == "client_tool_call_unavailable"
    assert target is None and thread is None
    async def live(*args): return "live-target", "live-thread"
    monkeypatch.setattr(cluster, "pending_route", live)
    original, target, thread = asyncio.run(cluster.recoverable_pending_route(
        request, principal, "chat.completions", None, audit))
    assert original is request and target == "live-target" and thread == "live-thread"


def test_client_cannot_set_internal_recovery_flag():
    request = ResponseRequest.model_validate({"model": "gpt-6-sol", "input": "hello",
                                             "_detached_tool_recovery": "forged"})
    assert request._detached_tool_recovery is None


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("stream", [False, True])
def test_http_recovery_keeps_original_audit_and_returns_normal_response(monkeypatch, endpoint, stream):
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from codex_gateway.main import app, get_backend
    from codex_gateway.backend import MockBackend
    from codex_gateway.auth import ApiPrincipal, require_api_key
    from codex_gateway.audit import current_audit
    from codex_gateway.database import SessionLocal
    from codex_gateway.models import ApiKey, UsageRecord
    seen = []
    class LostBackend(MockBackend):
        def continuation_target(self, request, key):
            raise ToolProtocolError("expired", "client_tool_call_unavailable")
        def check(self, request):
            assert request._detached_tool_recovery
            assert request.tools == [] and request.tool_choice == "none"
            assert request.previous_response_id is None and not tool_outputs(request)
            seen.append(request)
        async def complete(self, request, target):
            self.check(request)
            return await super().complete(request, target)
        async def stream(self, request, target):
            self.check(request)
            async for event in super().stream(request, target):
                yield event
    backend = LostBackend()
    async def seed():
        async with SessionLocal() as db:
            key = ApiKey(name="detached-test", prefix=uuid4().hex[:20], key_hash=uuid4().hex * 2)
            db.add(key)
            await db.commit()
            return key.id
    with TestClient(app) as client:
        key = client.portal.call(seed)
        async def auth():
            principal = ApiPrincipal(key, "test")
            current_audit.get()["principal"] = principal
            return principal
        app.dependency_overrides[require_api_key] = auth
        app.dependency_overrides[get_backend] = lambda: backend
        try:
            chat = ChatCompletionRequest(model="gpt-6-sol", stream=stream, messages=[
                {"role": "user", "content": "Run task once"},
                {"role": "assistant", "tool_calls": [{"id": "lost", "type": "function",
                    "function": {"name": "task", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "lost", "content": "Already completed"},
            ])
            body = (chat if endpoint == "chat.completions" else chat.to_response_request()).model_dump(mode="json")
            response = client.post("/v1/" + endpoint.replace(".", "/"), json=body)
            assert response.status_code == 200, response.text
            assert seen
            if stream:
                assert "text/event-stream" in response.headers["content-type"]
                assert ("response.completed" if endpoint == "responses" else "[DONE]") in response.text
                assert '"type":"error"' not in response.text
            else:
                assert response.json()["object"] == ("response" if endpoint == "responses" else "chat.completion")
            async def verify():
                async with SessionLocal() as db:
                    row = await db.scalar(select(UsageRecord).where(UsageRecord.api_key_id == key))
                    assert row.status_code == 200
                    assert row.request_params == body
                    assert row.conversation_evidence["execution"]["reason"] == "detached_tool_recovery"
            client.portal.call(verify)
        finally:
            app.dependency_overrides.pop(require_api_key, None)
            app.dependency_overrides.pop(get_backend, None)


