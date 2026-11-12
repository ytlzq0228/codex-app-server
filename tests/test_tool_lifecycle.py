"""Lifecycle regressions: release real resources, preserve user input, bound stalled waits."""
import asyncio
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from codex_gateway import cluster
from codex_gateway.backend import BackendTarget
from codex_gateway.client_tools import ToolProtocolError, result_before_user_turn, terminal_tool_call
from codex_gateway.config import get_settings, Settings
from codex_gateway.schemas import ResponseRequest, BackendStreamEvent
from codex_gateway.tool_sessions import ToolSessions

TOOL = {"type": "function", "name": "lookup", "parameters": {"type": "object"}}
CALL = {"type": "function_call", "name": "lookup", "call_id": "call-test",
        "arguments": "{}", "id": "fc-test", "status": "completed"}


def body(tool=TOOL, **kwargs):
    return ResponseRequest(model="gpt-6-sol", input="hello", tools=[tool], **kwargs)


def replacement(call=CALL):
    return ResponseRequest(model="gpt-6-sol", tools=[TOOL], input=[
        {"role": "user", "content": "Inspect the screen"},
        dict(call),
        {"type": "function_call_output", "call_id": call["call_id"], "output": "Tapped"},
        {"role": "user", "content": [{"type": "input_text", "text": "Now inspect this screen"},
                                   {"type": "input_image", "image_url": "https://example.com/screen.png"}]},
    ])


def audit_for(request):
    return {"body": json.dumps(request.model_dump(mode="json")).encode(),
            "body_complete": True, "body_bytes_received": 1, "body_hash": hashlib.sha256(),
            "transport": {"headers": []}}


async def pending_store(key="key", **kwargs):
    closed = asyncio.Event()
    sessions = None

    async def events(req, target, run):
        try:
            await sessions.await_result(run, CALL)
            yield BackendStreamEvent(tool_call=CALL, thread_id="thread-test")
            await sessions.receive_result(run)
            yield BackendStreamEvent(done=True, thread_id="thread-test")
        finally:
            closed.set()

    sessions = ToolSessions(events, **kwargs)
    target = BackendTarget(f"{key}:worker", "ws://worker", "/workspace")
    await cluster.collect(sessions.stream(body(), target))
    return sessions, target, closed


@pytest.mark.asyncio
async def test_replacement_releases_before_new_turn_and_preserves_image():
    sessions, target, closed = await pending_store(per_key_limit=1)
    request = replacement()
    original = request.model_dump()
    assert await sessions.replace_result_turn(request, "key")
    assert closed.is_set()
    assert not sessions.runs and not sessions.pending
    assert request.model_dump() == original
    assert request.input[-1]["content"][1]["type"] == "input_image"
    assert sessions.retired[("key", "call-test")][1] == "client_tool_result_duplicate"
    await sessions.close()


@pytest.mark.parametrize("mutation", ["foreign", "call", "model", "tools", "duplicate", "claimed"])
@pytest.mark.asyncio
async def test_replacement_never_cancels_wrong_or_claimed_call(mutation):
    sessions, target, closed = await pending_store()
    request = replacement()
    key = "key"
    if mutation == "foreign":
        key = "other"
    elif mutation == "call":
        request.input[1]["arguments"] = '{"changed":true}'
    elif mutation == "model":
        request.model = "gpt-6-astra"
    elif mutation == "tools":
        request.tools = [{"type": "function", "name": "different"}]
    elif mutation == "duplicate":
        request.input.insert(2, dict(request.input[2]))
    else:
        next(iter(sessions.runs)).claimed = True
    try:
        with pytest.raises(ToolProtocolError):
            await sessions.replace_result_turn(request, key)
        assert not closed.is_set() and len(sessions.runs) == 1
    finally:
        await sessions.close()


@pytest.mark.asyncio
async def test_isolated_ingress_confirms_local_cleanup():
    key = uuid4()
    sessions, target, closed = await pending_store(str(key))
    request = replacement()
    backend = SimpleNamespace(tool_sessions=sessions,
        continuation_target=sessions.target_for,
        continuation_thread=lambda req, k: sessions.find(req, k).thread_id)
    audit = audit_for(request)
    await cluster.release_replaced_result(request, SimpleNamespace(key_id=key), "chat.completions", backend, audit)
    assert closed.is_set() and not sessions.runs
    assert audit["tool_lifecycle"]["action"] == "replace_result_turn"
    await sessions.close()


@pytest.mark.asyncio
async def test_replacement_retry_waits_for_existing_cleanup(monkeypatch):
    key = uuid4()
    sessions, target, closed = await pending_store(str(key))
    started, release = asyncio.Event(), asyncio.Event()
    original = sessions.replace_result_turn
    calls = []
    async def delayed(request, owner):
        calls.append(owner)
        await original(request, owner)
        started.set()
        await release.wait()
        return True
    monkeypatch.setattr(sessions, 'replace_result_turn', delayed)
    backend = SimpleNamespace(tool_sessions=sessions,
        continuation_target=sessions.target_for,
        continuation_thread=lambda req, k: sessions.find(req, k).thread_id)
    request = replacement()
    async def ingress():
        await cluster.release_replaced_result(request, SimpleNamespace(key_id=key),
            'responses', backend, audit_for(request))
    first = asyncio.create_task(ingress())
    await asyncio.wait_for(started.wait(), 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    retry = asyncio.create_task(ingress())
    await asyncio.sleep(.01)
    assert not retry.done()
    release.set()
    await asyncio.wait_for(retry, 1)
    assert len(calls) == 1 and closed.is_set() and not sessions.runs
    await sessions.close()


@pytest.mark.asyncio
async def test_remote_replacement_requires_owner_ack(monkeypatch):
    request = replacement()
    target = BackendTarget("key:worker", "ws://worker", "/workspace")
    async def route(*args):
        return target, "thread-test"
    async def owner(*args):
        return "http://example.com"
    seen = []
    async def cancel(url, action, payload, settings):
        seen.append((action, payload))
        return False
    monkeypatch.setattr(cluster, "pending_route", route)
    monkeypatch.setattr(cluster, "owner_url", owner)
    monkeypatch.setattr(cluster, "request_cancellation", cancel)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:
        await cluster.release_replaced_result(request, SimpleNamespace(key_id=uuid4()),
                                             "responses", SimpleNamespace(), audit_for(request))
    assert caught.value.status_code == 503
    assert seen[0][0] == "replace-result"
    assert seen[0][1]["request"]["input"][-1] == request.input[-1]


@pytest.mark.asyncio
async def test_terminal_tool_closes_producer_before_return_without_route(monkeypatch):
    monkeypatch.setattr(get_settings(), "structured_output_tool_terminal", True)
    closed = asyncio.Event()
    sessions = None
    call = {**CALL, "name": "StructuredOutput"}
    async def publish(*args):
        pytest.fail("A terminal output must not publish a pending route")
    monkeypatch.setattr(cluster, "publish_tool", publish)
    async def events(req, target, run):
        try:
            await sessions.await_result(run, call)
            yield BackendStreamEvent(tool_call=call, thread_id="thread-test", output_tokens=12)
            pytest.fail("A terminal output must not wait for a client result")
        finally:
            closed.set()
    sessions = ToolSessions(events, per_key_limit=1)
    target = BackendTarget("key:worker", "ws://worker", "/workspace")
    for _ in range(3):
        result = await cluster.collect(sessions.stream(body({**TOOL, "name": "StructuredOutput"}), target))
        assert result.terminal_tool and result.tool_calls == [call]
        assert result.output_tokens == 12 and closed.is_set()
        assert not sessions.runs and not sessions.pending
    await sessions.close()


def test_terminal_policy_optout_explicit_contract_and_schema(monkeypatch):
    request = body({**TOOL, "name": "StructuredOutput"})
    call = {**CALL, "name": "StructuredOutput"}
    monkeypatch.setattr(get_settings(), "structured_output_tool_terminal", False)
    assert not terminal_tool_call(request, call)
    request.metadata = {"gateway_tool_lifecycle": "terminal"}
    assert terminal_tool_call(request, call)
    request.metadata["gateway_tool_lifecycle"] = "interactive"
    assert not terminal_tool_call(request, call)
    monkeypatch.setattr(get_settings(), "structured_output_tool_terminal", True)
    request.metadata = None
    request.tools.append(TOOL)
    assert not terminal_tool_call(request, call)
    request.tools.pop()
    with pytest.raises(ToolProtocolError, match="JSON Schema"):
        terminal_tool_call(request, {**call, "arguments": "invalid"})
    assert not terminal_tool_call(body(), CALL)


@pytest.mark.asyncio
async def test_upstream_stall_releases_session_and_resource():
    closed = asyncio.Event()
    async def events(req, target, run):
        try:
            await asyncio.Event().wait()
            yield BackendStreamEvent(done=True)
        finally:
            closed.set()
    sessions = ToolSessions(events, progress_timeout=.02)
    target = BackendTarget("key:worker", "ws://worker", "/workspace")
    with pytest.raises(ToolProtocolError) as caught:
        await asyncio.wait_for(cluster.collect(sessions.stream(body(), target)), 1)
    assert caught.value.code == "client_tool_execution_timeout"
    assert closed.is_set() and not sessions.runs and not sessions.pending
    await sessions.close()


@pytest.mark.asyncio
async def test_empty_heartbeats_do_not_extend_progress_deadline():
    async def events(req, target, run):
        while True:
            yield BackendStreamEvent(thread_id="thread-test")
            await asyncio.sleep(.003)
    sessions = ToolSessions(events, progress_timeout=.02)
    target = BackendTarget("key:worker", "ws://worker", "/workspace")
    with pytest.raises(ToolProtocolError) as caught:
        await asyncio.wait_for(cluster.collect(sessions.stream(body(), target)), 1)
    assert caught.value.code == "client_tool_execution_timeout"
    assert not sessions.runs
    await sessions.close()


@pytest.mark.asyncio
async def test_live_progress_can_outlast_deadline():
    async def events(req, target, run):
        for _ in range(10):
            yield BackendStreamEvent(delta="x", thread_id="thread-test")
            await asyncio.sleep(.005)
        yield BackendStreamEvent(done=True, thread_id="thread-test")
    sessions = ToolSessions(events, progress_timeout=.03)
    result = await cluster.collect(sessions.stream(body(), BackendTarget("key:worker", "ws://worker", "/workspace")))
    assert result.text == "x" * 10 and not sessions.runs
    await sessions.close()


@pytest.mark.asyncio
async def test_waiting_result_has_own_deadline_and_expiry_cleanup():
    sessions, target, closed = await pending_store(ttl=.06, progress_timeout=.01)
    await asyncio.sleep(.03)
    assert sessions.runs and not closed.is_set()
    await asyncio.wait_for(closed.wait(), 1)
    await asyncio.sleep(0)
    assert not sessions.pending and not sessions.runs
    assert sessions.retired[("key", "call-test")][1] == "client_tool_call_expired"
    await sessions.close()


@pytest.mark.asyncio
async def test_abandoned_consumer_cannot_block_full_queue_forever():
    closed = asyncio.Event()
    async def events(req, target, run):
        try:
            while True:
                yield BackendStreamEvent(delta="x", thread_id="thread-test")
        finally:
            closed.set()
    sessions = ToolSessions(events, consumer_timeout=.02, progress_timeout=1)
    stream = sessions.stream(body(), BackendTarget("key:worker", "ws://worker", "/workspace"))
    await anext(stream)
    await asyncio.wait_for(closed.wait(), 1)
    await asyncio.sleep(0)
    assert not sessions.runs
    with pytest.raises(ToolProtocolError) as caught:
        await anext(stream)
    assert caught.value.code == "client_tool_consumer_timeout"
    await stream.aclose()
    await sessions.close()

@pytest.mark.asyncio
async def test_cancelled_full_queue_reports_failure_without_hanging():
    async def events(req, target, run):
        while True:
            yield BackendStreamEvent(delta="x")
    sessions = ToolSessions(events, consumer_timeout=1)
    stream = sessions.stream(body(), BackendTarget("key:worker", "ws://worker", "/workspace"))
    await anext(stream)
    run = next(iter(sessions.runs))
    async def full():
        while not run.queue.full():
            await asyncio.sleep(0)
    await asyncio.wait_for(full(), 1)
    await sessions.close()
    with pytest.raises(ToolProtocolError) as caught:
        await asyncio.wait_for(anext(stream), 1)
    assert caught.value.code == "client_tool_call_unavailable"
    assert not sessions.runs and not sessions.pending
    await stream.aclose()


def test_client_reply_deadline_cannot_exceed_worker_limit():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(client_tool_result_timeout_seconds=301)


@pytest.mark.parametrize("endpoint", ["responses", "chat/completions"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
def test_http_lifecycle_audit_and_release(endpoint, stream, terminal, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from codex_gateway.main import app, get_backend
    from codex_gateway.backend import MockBackend
    from codex_gateway.auth import ApiPrincipal, require_api_key
    from codex_gateway.audit import current_audit
    from codex_gateway.database import SessionLocal
    from codex_gateway.models import ApiKey, UsageRecord, ResponseBinding
    from codex_gateway.schemas import ChatCompletionRequest

    monkeypatch.setattr(get_settings(), "structured_output_tool_terminal", True)
    tool = {**TOOL, "name": "StructuredOutput"} if terminal else TOOL
    call = {**CALL, "name": tool["name"]}
    closed = []
    seen = []

    class Backend(MockBackend):
        def __init__(self):
            self.tool_sessions = ToolSessions(self.events, per_key_limit=1)
        def continuation_target(self, req, key):
            return self.tool_sessions.target_for(req, key)
        def continuation_thread(self, req, key):
            run = self.tool_sessions.find(req, key)
            return run.thread_id if run else None
        async def events(self, req, target, run):
            seen.append(req.model_copy(deep=True))
            try:
                if isinstance(req.input, list) and req.input[-1].get("role") == "user" and len(req.input) > 1:
                    yield BackendStreamEvent(delta="new screen handled", done=True, thread_id="thread-new")
                    return
                await self.tool_sessions.await_result(run, call)
                yield BackendStreamEvent(tool_call=call, thread_id="thread-test", output_tokens=12)
                await self.tool_sessions.receive_result(run)
                yield BackendStreamEvent(done=True, thread_id="thread-test")
            finally:
                closed.append(True)
        async def stream(self, req, target):
            async for event in self.tool_sessions.stream(req, target):
                yield event
        async def complete(self, req, target):
            return await cluster.collect(self.stream(req, target))
    backend = Backend()

    async def seed():
        async with SessionLocal() as db:
            key = ApiKey(name="lifecycle-test", prefix=uuid4().hex[:20], key_hash=uuid4().hex * 2)
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
            first = {"model": "gpt-6-sol", "stream": stream}
            if endpoint == "responses":
                first.update(input=[{"role": "user", "content": "Inspect the screen"}], tools=[tool])
            else:
                first.update(messages=[{"role": "user", "content": "Inspect the screen"}],
                             tools=[{"type": "function", "function": {k: v for k, v in tool.items() if k != "type"}}])
            response = client.post("/v1/" + endpoint, json=first)
            assert response.status_code == 200, response.text
            assert call["call_id"] in response.text
            if terminal:
                assert closed and not backend.tool_sessions.runs
            else:
                assert len(backend.tool_sessions.runs) == 1
                if endpoint == "responses":
                    second = replacement().model_dump(mode="json")
                else:
                    second = {**first, "messages": [
                        first["messages"][0],
                        {"role": "assistant", "tool_calls": [{"id": call["call_id"], "type": "function",
                         "function": {"name": "lookup", "arguments": "{}"}}]},
                        {"role": "tool", "tool_call_id": call["call_id"], "content": "Tapped"},
                        {"role": "user", "content": "Now inspect this screen"}]}
                second["stream"] = stream
                response = client.post("/v1/" + endpoint, json=second)
                assert response.status_code == 200, response.text
                assert "new screen handled" in response.text
                assert len(closed) == 2 and not backend.tool_sessions.runs
                assert seen[-1].input[-1]["role"] == "user"
            async def verify():
                async with SessionLocal() as db:
                    rows = (await db.scalars(select(UsageRecord).where(UsageRecord.api_key_id == key)
                                            .order_by(UsageRecord.created_at))).all()
                    assert rows[-1].conversation_evidence["execution_outcome"] == "completed"
                    assert rows[-1].conversation_evidence["tool_lifecycle"]["action"] == (
                        "terminal_output" if terminal else "replace_result_turn")
                    if terminal:
                        assert not (await db.scalars(select(ResponseBinding).where(ResponseBinding.api_key_id == key))).all()
            client.portal.call(verify)
        finally:
            client.portal.call(backend.tool_sessions.close)
            app.dependency_overrides.pop(require_api_key, None)
            app.dependency_overrides.pop(get_backend, None)


@pytest.mark.parametrize("adapter", ["codex", "claude", "gemini"])
@pytest.mark.asyncio
async def test_disabled_tools_do_not_consume_session_capacity(adapter, monkeypatch):
    from codex_gateway.backend import AppServerBackend
    from codex_gateway.claude_backend import ClaudeAdapter
    from codex_gateway.gemini_backend import GeminiAdapter
    backend = {"codex": AppServerBackend, "claude": ClaudeAdapter, "gemini": GeminiAdapter}[adapter](Settings())
    async def unexpected(*args):
        pytest.fail("tool_choice=none must bypass session admission")
        yield
    async def events(*args):
        yield BackendStreamEvent(done=True, thread_id="test")
    monkeypatch.setattr(backend.tool_sessions, "stream", unexpected)
    monkeypatch.setattr(backend, "_turn_events", events)
    target = BackendTarget("key:worker", "ws://worker", "/workspace", provider=adapter)
    method = backend._turn if adapter == "codex" else backend.stream
    result = await cluster.collect(method(body(tool_choice="none"), target))
    assert result.thread_id == "test"


