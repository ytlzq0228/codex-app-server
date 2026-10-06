"""Regression cases for abandoning execution versus resuming an authenticated RPC."""
import copy
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from codex_gateway import execution as ex, cluster
from codex_gateway.auth import ApiPrincipal
from codex_gateway.client_tools import ToolProtocolError, compatible_definitions, definitions
from codex_gateway.config import get_settings
from codex_gateway.database import SessionLocal
from codex_gateway.main import app
from codex_gateway.models import ExecutionSession
from codex_gateway.schemas import ResponseRequest
from test_execution import audit_for
from test_tool_recovery import setup_waiting


@pytest.mark.parametrize("case", ["null_checkpoint", "missing_result", "new_tools", "summary_only", "claimed"])
def test_new_user_turn_abandons_without_replaying_tools(monkeypatch, case):
    monkeypatch.setattr(get_settings(), "model_providers", "claude-recovery:claude")
    async def run():
        p, w, t, first, follow, cid, logical, thread, sessions = await setup_waiting()
        async with SessionLocal() as db:
            row = await db.get(ExecutionSession, logical)
            row.history_hashes = None
            await db.commit()
        if case == "missing_result":
            follow.input = [i for i in follow.input if i.get("type") != "function_call_output"]
        if case == "new_tools":
            follow.input.append({"type": "additional_tools", "tools": [
                {"type": "function", "name": "new_tool", "parameters": {"type": "object"}}]})
        if case == "summary_only":
            follow.input = [{"role": "user", "content": "Continue from this summary: DNS inspection was cancelled."}]
        if case == "claimed":
            next(iter(sessions.runs)).claimed = True
        follow.input.append({"role": "developer", "content": "reminder"})
        original = copy.deepcopy(follow.input)
        audit = audit_for(follow, cid)
        try:
            if case == "claimed":
                with pytest.raises(HTTPException):
                    await ex.prepare(follow, p, "responses", audit, tool_sessions=sessions)
                assert sessions.has_pending(p.key_id, thread)
                assert "execution" not in audit
            else:
                prepared, binding = await ex.prepare(follow, p, "responses", audit, tool_sessions=sessions)
                assert binding is None and not prepared.previous_response_id
                assert not definitions(prepared) and prepared.tool_choice == "none"
                assert ex.RECOVERY_NOTICE in prepared.input_text()
                assert prepared.input[-1] == original[-1]
                assert follow.input == original
                assert not sessions.has_pending(p.key_id, thread)
                assert audit["execution_decision"]["reason"] == "context_only_recovery"
        finally:
            await ex.cleanup(audit)
            await sessions.close()
    with TestClient(app) as client:
        client.portal.call(run)


@pytest.mark.parametrize("case", ["ok", "changed_tools", "arguments", "foreign_key", "wrong_session", "stale", "null_checkpoint"])
def test_lost_result_requires_persisted_scoped_proof(monkeypatch, case):
    monkeypatch.setattr(get_settings(), "model_providers", "claude-recovery:claude")
    async def run():
        p, w, t, first, follow, cid, logical, thread, sessions = await setup_waiting()
        follow.input = follow.input[:4] + [{"role": "developer", "content": "reminder"}]
        if case != "changed_tools":
            await sessions.close()
        else:
            follow.tools.append({"type": "function", "name": "new_tool", "parameters": {"type": "object"}})
        if case == "arguments":
            follow.input[2]["arguments"] = '{"a":123}'
        if case == "foreign_key":
            p = ApiPrincipal(uuid4(), "test")
        if case == "wrong_session":
            cid = str(uuid4())
        if case == "null_checkpoint":
            async with SessionLocal() as db:
                row = await db.get(ExecutionSession, logical)
                row.history_hashes = None
                await db.commit()
        audit = audit_for(follow, cid)
        backend = SimpleNamespace(continuation_target=lambda request, key: sessions.target_for(request, key))
        try:
            if case in {"arguments", "foreign_key", "wrong_session", "null_checkpoint"}:
                with pytest.raises(ToolProtocolError) as error:
                    await cluster.recoverable_pending_route(follow, p, "responses", backend, audit)
                assert error.value.code == "client_tool_call_unavailable"
            else:
                recovered, target, pending = await cluster.recoverable_pending_route(follow, p, "responses", backend, audit)
                assert target is None and pending is None
                if case == "stale":
                    async with SessionLocal() as db:
                        row = await db.get(ExecutionSession, logical)
                        row.response_id = "newer-response"
                        await db.commit()
                    with pytest.raises(HTTPException):
                        await ex.prepare(recovered, p, "responses", audit, tool_sessions=sessions)
                    assert "execution" not in audit
                else:
                    prepared, binding = await ex.prepare(recovered, p, "responses", audit, tool_sessions=sessions)
                    assert binding is None and not definitions(prepared)
                    assert prepared.tool_choice == "none"
                    assert audit["execution_decision"]["reason"] == "context_only_recovery"
        finally:
            await ex.cleanup(audit)
            await sessions.close()
    with TestClient(app) as client:
        client.portal.call(run)


@pytest.mark.parametrize("case", ["auth", "key", "response", "thread", "state", "ok"])
def test_abandon_owner_authentication_and_execution_identity(monkeypatch, case):
    monkeypatch.setattr(get_settings(), "model_providers", "claude-recovery:claude")
    async def run():
        p, w, t, first, follow, cid, logical, thread, sessions = await setup_waiting(owner="abandon-owner")
        settings = get_settings().model_copy(update={"node_id": "abandon-owner"})
        async with SessionLocal() as db:
            row = await db.get(ExecutionSession, logical)
            payload = {"logical_id": logical, "key_id": str(p.key_id),
                       "response_id": row.response_id, "thread_id": thread}
            if case == "state":
                row.state = "running"
            await db.commit()
        if case == "key": payload["key_id"] = str(uuid4())
        if case == "response": payload["response_id"] = "stale"
        if case == "thread": payload["thread_id"] = "foreign"
        owner_app = FastAPI()
        owner_app.include_router(cluster.router)
        owner_app.state.backend = SimpleNamespace(tool_sessions=sessions)
        monkeypatch.setattr(cluster, "get_settings", lambda: settings)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://owner") as client:
                token = "wrong" if case == "auth" else settings.manager_token.get_secret_value()
                response = await client.post("/internal/tools/abandon", json=payload,
                    headers={"Authorization": "Bearer " + token})
            assert response.status_code == (200 if case == "ok" else 401 if case == "auth" else 409)
            assert sessions.has_pending(p.key_id, thread) == (case != "ok")
        finally:
            await sessions.close()
    with TestClient(app) as client:
        client.portal.call(run)


def test_tool_contracts_ignore_order_and_description_but_not_schema():
    tools = [{"type": "function", "name": name, "parameters": {"type": "object", "properties": {}}}
             for name in ("lookup", "read")]
    original = ResponseRequest(model="claude-recovery", input="test", tools=tools)
    changed = original.model_copy(deep=True)
    changed.tools.reverse()
    changed.tools[0]["description"] = "Updated display description"
    assert compatible_definitions(changed, original)
    changed.tools[0]["parameters"]["required"] = ["path"]
    assert not compatible_definitions(changed, original)
    assert original.tools == tools


@pytest.mark.parametrize("case", ["ok", "changed_output", "foreign_key", "wrong_session", "explicit_response", "stale", "claimed"])
def test_failed_tool_retry_recovers_only_verified_context(monkeypatch, case):
    monkeypatch.setattr(get_settings(), "model_providers", "claude-recovery:claude")
    async def run():
        p, w, t, first, follow, cid, logical, thread, sessions = await setup_waiting()
        follow.input = follow.input[:4]
        failed_audit = audit_for(follow, cid)
        await ex.prepare(follow, p, "responses", failed_audit, pending_thread=thread, tool_sessions=sessions)
        async with SessionLocal() as db:
            await ex.finish(db, failed_audit, None, t, "resp_failed_test")
            await db.commit()
        await ex.cleanup(failed_audit)
        await sessions.close()
        if case == "changed_output":
            follow.input[-1]["output"] = "forged"
        if case == "foreign_key":
            p = ApiPrincipal(uuid4(), "test")
        if case == "wrong_session":
            cid = str(uuid4())
        if case == "explicit_response":
            follow.previous_response_id = "resp_failed_test"
        if case == "claimed":
            async with SessionLocal() as db:
                row = await db.get(ExecutionSession, logical)
                row.lease_token = "another-owner"
                from datetime import timedelta
                row.lease_until = ex.now() + timedelta(seconds=60)
                await db.commit()
        audit = audit_for(follow, cid)
        recovered = await ex.recover_lost_output(follow, p, "responses", audit, failed_only=True)
        if case in {"changed_output", "foreign_key", "wrong_session", "explicit_response", "claimed"}:
            assert recovered is None
            return
        assert recovered is not None
        # A stale owner route must not be consulted once the failure is proven.
        backend = SimpleNamespace(continuation_target=lambda *_: pytest.fail("old RPC resumed"))
        routed, target, routed_thread = await cluster.recoverable_pending_route(
            follow, p, "responses", backend, audit)
        assert target is None and routed_thread is None and routed._recovery_response_id
        if case == "stale":
            async with SessionLocal() as db:
                row = await db.get(ExecutionSession, logical)
                row.response_id = "resp_replaced"
                await db.commit()
            with pytest.raises(HTTPException):
                await ex.prepare(recovered, p, "responses", audit)
            return
        try:
            prepared, binding = await ex.prepare(recovered, p, "responses", audit)
            assert binding is None
            assert not definitions(prepared) and prepared.tool_choice == "none"
            assert ex.RECOVERY_NOTICE in prepared.input_text()
            assert audit["execution_decision"]["reason"] == "context_only_recovery"
        finally:
            await ex.cleanup(audit)
    with TestClient(app) as client:
        client.portal.call(run)
