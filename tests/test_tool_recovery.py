import asyncio
import copy
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException, FastAPI
from fastapi.testclient import TestClient

from codex_gateway import execution as ex, cluster
from codex_gateway.auth import ApiPrincipal
from codex_gateway.backend import BackendTarget
from codex_gateway.config import get_settings
from codex_gateway.database import SessionLocal
from codex_gateway.main import app
from codex_gateway.models import ApiKey, Worker, ExecutionSession, PendingToolRoute, AppNode
from codex_gateway.schemas import ResponseRequest, BackendResult, BackendStreamEvent
from codex_gateway.tool_sessions import ToolSessions
from test_execution import audit_for


def call(arguments='{"a":1,"b":"中文"}'):
    return {"type": "function_call", "call_id": "call-recovery", "name": "Bash", "arguments": arguments}


def test_json_checkpoint_semantics_and_legacy_preimage():
    original = call()
    reformatted = call(' { "b": "\\u4e2d\\u6587", "a": 1 } ')
    assert ex.hashes([original]) == ex.hashes([reformatted])
    for old in (original, call(json.dumps({"b": "中文", "a": 1}))):
        saved = [ex.digest(ex.normal_item(old, legacy=True))]
        assert ex.checkpoint_matches([reformatted], saved)
    for field, value in (("call_id", "other"), ("name", "Other"), ("arguments", '{"a":2,"b":"中文"}')):
        edited = {**reformatted, field: value}
        assert not ex.checkpoint_matches([edited], ex.hashes([original]))
    assert not ex.checkpoint_matches([], ex.hashes([original]))
    assert ex.hashes([call('{"a":[1,2]}')]) != ex.hashes([call('{"a":[2,1]}')])
    # Ambiguous JSON, arbitrary custom-tool text and ordinary text stay byte-sensitive.
    for first, second in [('{"a":1,"a":2}', '{"a":2}'), ('{"a":NaN}', '{ "a":NaN}'),
                          ('{"a":1e999}', '{ "a":1e999}')]:
        assert ex.hashes([call(first)]) != ex.hashes([call(second)])
    for kind in ("custom_tool_call",):
        assert ex.hashes([{**original, "type": kind}]) != ex.hashes([{**reformatted, "type": kind}])
    assert ex.hashes([{"role": "user", "content": "a b"}]) != ex.hashes([{"role": "user", "content": "ab"}])


async def setup_waiting(legacy=True, owner=""):
    async with SessionLocal() as db:
        key = ApiKey(name="recovery", prefix=uuid4().hex[:20], key_hash=uuid4().hex * 2)
        worker = Worker(name=uuid4().hex, container_name=uuid4().hex, endpoint="http://worker",
                        status="ready", enabled=True, provider="claude", node_id=owner or None)
        db.add_all([key, worker])
        await db.commit()
    principal = ApiPrincipal(key.id, "test")
    first = ResponseRequest(model="claude-recovery", input=[{"role": "user", "content": "Inspect DNS"}],
                            tools=[{"type": "function", "name": "Bash", "parameters": {"type": "object"}}])
    client_id = str(uuid4())
    audit = audit_for(first, client_id)
    await ex.prepare(first, principal, "responses", audit)
    thread = str(uuid4())
    target = BackendTarget(str(key.id) + ":" + str(worker.id), worker.endpoint, "/tmp",
                           worker.id, worker.execution_generation, "claude")
    prefix = [*first.input, {"role": "assistant", "content": "I will inspect DNS"}, call()]
    async with SessionLocal() as db:
        await ex.finish(db, audit, BackendResult(text="I will inspect DNS", tool_calls=[call()], thread_id=thread),
                        target, "resp_" + uuid4().hex)
        if legacy:
            row = await db.get(ExecutionSession, audit["execution"]["logical_id"])
            row.history_hashes = [ex.digest(ex.normal_item(i, legacy=True)) for i in prefix]
        await db.commit()
    await ex.cleanup(audit)
    follow = first.model_copy(deep=True)
    follow.input = [*copy.deepcopy(prefix[:-1]), call(' { "b": "中文", "a": 1 } '),
                    {"type": "function_call_output", "call_id": call()["call_id"], "output": "User rejected the tool", "is_error": True},
                    {"role": "user", "content": "[Request interrupted by user for tool use]"},
                    {"role": "assistant", "content": "No response requested."},
                    {"role": "user", "content": "继续"}]
    async def events(request, target, run):
        await sessions.await_result(run, call())
        yield BackendStreamEvent(tool_call=call(), thread_id=thread)
        await sessions.receive_result(run)
        pytest.fail("Rejected tool must never be resumed")
    sessions = ToolSessions(events)
    await anext(sessions.stream(first, target))
    return principal, worker, target, first, follow, client_id, audit["execution"]["logical_id"], thread, sessions


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("expired", [True, False])
def test_rejected_tool_then_continue_rebuilds_verified_history(monkeypatch, legacy, expired):
    monkeypatch.setattr(get_settings(), "model_providers", "claude-recovery:claude")
    async def run():
        p, w, t, first, follow, cid, logical, thread, sessions = await setup_waiting(legacy)
        if expired:
            await sessions.close()
            async with SessionLocal() as db:
                row = await db.get(ExecutionSession, logical)
                row.expires_at = ex.now() - timedelta(seconds=1)
                await db.commit()
        audit = audit_for(follow, cid)
        try:
            prepared, binding = await ex.prepare(follow, p, "responses", audit, tool_sessions=sessions)
            assert binding is None and prepared.previous_response_id is None
            assert prepared.input == follow.input
            assert prepared.input[-1]["content"] == "继续"
            assert prepared.input[3]["is_error"] is True
            assert audit["execution_decision"]["reason"] == ("pending_tool_lost" if expired else "pending_tool_superseded")
            assert audit["execution"]["history"] == ex.hashes(follow.input)
            assert not sessions.has_pending(p.key_id, thread)
        finally:
            await ex.cleanup(audit)
            await sessions.close()
    with TestClient(app) as client:
        client.portal.call(run)


@pytest.mark.parametrize("tamper", ["arguments", "call_id", "duplicate", "partial", "tool_name"])
@pytest.mark.parametrize("expired", [False, True])
def test_recovery_does_not_bypass_tool_or_history_checks(monkeypatch, tamper, expired):
    monkeypatch.setattr(get_settings(), "model_providers", "claude-recovery:claude")
    async def run():
        p, w, t, first, follow, cid, logical, thread, sessions = await setup_waiting()
        if expired:
            await sessions.close()
        if tamper == "arguments": follow.input[2]["arguments"] = '{"a":2,"b":"中文"}'
        if tamper == "call_id": follow.input[3]["call_id"] = "foreign"
        if tamper == "duplicate": follow.input.insert(4, copy.deepcopy(follow.input[3]))
        if tamper == "partial": follow.input.pop(0)
        if tamper == "tool_name": follow.input[2]["name"] = "Other"
        try:
            with pytest.raises(HTTPException) as error:
                await ex.prepare(follow, p, "responses", audit_for(follow, cid), tool_sessions=sessions)
            assert error.value.status_code == 409
            assert sessions.has_pending(p.key_id, thread) == (not expired)
        finally:
            await sessions.close()
    with TestClient(app) as client:
        client.portal.call(run)


@pytest.mark.parametrize("failure", [None, "timeout"])
def test_cross_node_recovery_cancels_on_owner_before_rebuild(monkeypatch, failure):
    from contextvars import ContextVar
    from dataclasses import asdict
    monkeypatch.setattr(get_settings(), "model_providers", "claude-recovery:claude")
    async def run():
        p, w, target, first, follow, cid, logical, thread, sessions = await setup_waiting(owner="recovery-owner")
        settings = get_settings()
        ingress = settings.model_copy(update={"node_id": "recovery-ingress"})
        owner = settings.model_copy(update={"node_id": "recovery-owner"})
        async with SessionLocal() as db:
            node = await db.get(AppNode, owner.node_id)
            if not node:
                node = AppNode(id=owner.node_id, gateway_url="http://owner", manager_url="http://manager")
                db.add(node)
            node.heartbeat_at = ex.now()
            node.enabled = True
            data = asdict(target)
            data["worker_id"] = str(w.id)
            db.add(PendingToolRoute(key_id=str(p.key_id), call_id=call()["call_id"],
                node_id=owner.node_id, thread_id=thread, target=data, expires_at=ex.now() + timedelta(minutes=5)))
            await db.commit()
        remote = ContextVar("remote", default=False)
        owner_app = FastAPI()
        owner_app.include_router(cluster.router)
        owner_app.state.backend = SimpleNamespace(tool_sessions=sessions)
        real_client = httpx.AsyncClient
        seen = []
        class Transport(httpx.ASGITransport):
            async def handle_async_request(self, request):
                seen.append(request.url.path)
                token = remote.set(True)
                try:
                    if failure:
                        raise httpx.ReadTimeout("cancellation not confirmed")
                    return await super().handle_async_request(request)
                finally:
                    remote.reset(token)
        monkeypatch.setattr(cluster, "get_settings", lambda: owner if remote.get() else ingress)
        monkeypatch.setattr(ex, "get_settings", lambda: ingress)
        monkeypatch.setattr(cluster.httpx, "AsyncClient", lambda **kw: real_client(transport=Transport(app=owner_app), **kw))
        empty = ToolSessions(None)
        audit = audit_for(follow, cid)
        try:
            if failure:
                with pytest.raises(HTTPException) as error:
                    await ex.prepare(follow, p, "responses", audit, tool_sessions=empty)
                assert error.value.status_code == 503
                assert sessions.has_pending(p.key_id, thread)
                assert "execution" not in audit
            else:
                prepared, binding = await ex.prepare(follow, p, "responses", audit, tool_sessions=empty)
                assert binding is None and prepared.input == follow.input
                assert audit["execution_decision"]["reason"] == "pending_tool_superseded"
                assert not sessions.has_pending(p.key_id, thread)
                async with SessionLocal() as db:
                    assert await db.get(PendingToolRoute, (str(p.key_id), call()["call_id"])) is None
            assert seen == ["/internal/tools/supersede"]
        finally:
            await ex.cleanup(audit)
            await sessions.close()
    with TestClient(app) as client:
        client.portal.call(run)


@pytest.mark.parametrize("tamper", ["auth", "key", "response", "history", "generation", "call_id"])
def test_owner_cancellation_rejects_wrong_identity(monkeypatch, tamper):
    from dataclasses import asdict
    monkeypatch.setattr(get_settings(), "model_providers", "claude-recovery:claude")
    async def run():
        p, w, target, first, follow, cid, logical, thread, sessions = await setup_waiting(owner="owner-security")
        settings = get_settings().model_copy(update={"node_id": "owner-security"})
        async with SessionLocal() as db:
            row = await db.get(ExecutionSession, logical)
            payload = {"logical_id": logical, "key_id": str(p.key_id), "response_id": row.response_id,
                       "request": follow.model_dump(mode="json")}
            data = asdict(target)
            data["worker_id"] = str(w.id)
            if tamper == "generation": data["worker_generation"] += 1
            db.add(PendingToolRoute(key_id=str(p.key_id), call_id=call()["call_id"],
                node_id=settings.node_id, thread_id=thread, target=data, expires_at=ex.now() + timedelta(minutes=5)))
            await db.commit()
        if tamper == "key": payload["key_id"] = str(uuid4())
        if tamper == "response": payload["response_id"] = "stale"
        if tamper == "history": payload["request"]["input"][2]["arguments"] = '{"a":2}'
        if tamper == "call_id": payload["request"]["input"][3]["call_id"] = "foreign"
        owner_app = FastAPI()
        owner_app.include_router(cluster.router)
        owner_app.state.backend = SimpleNamespace(tool_sessions=sessions)
        monkeypatch.setattr(cluster, "get_settings", lambda: settings)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://owner") as client:
                token = "wrong" if tamper == "auth" else settings.manager_token.get_secret_value()
                response = await client.post("/internal/tools/supersede", json=payload,
                    headers={"Authorization": "Bearer " + token})
                assert response.status_code == (401 if tamper == "auth" else 409), response.text
                assert sessions.has_pending(p.key_id, thread)
        finally:
            await sessions.close()
    with TestClient(app) as client:
        client.portal.call(run)
