import asyncio
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from codex_gateway.config import get_settings
from codex_gateway.models import Base, ModelPrice, UsageRecord
from codex_gateway.schemas import BackendStreamEvent, ResponseRequest
from codex_gateway.tool_sessions import ToolSessions
from codex_gateway.usage_accounting import TOKEN_FIELDS, apply_usage
from test_claude_worker import worker


def test_partial_usage_deduplicates_messages_and_assistant_echoes(worker):
    usage = worker.TurnUsage()
    def start(mid, **counts):
        return {"type": "stream_event", "event": {"type": "message_start",
            "message": {"id": mid, "usage": counts}}}
    def delta(output):
        return {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": output}}}
    usage.observe(start("first", input_tokens=10, cache_read_input_tokens=20, cache_creation_input_tokens=5, output_tokens=1))
    usage.observe(delta(7))
    echo = {"type": "assistant", "message": {"id": "first", "usage": {
        "input_tokens": 10, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5, "output_tokens": 7}}}
    assert not usage.observe(echo)
    assert not usage.observe(echo)
    usage.observe(start("second", input_tokens=3, output_tokens=0))
    usage.observe(delta(4))
    assert usage.counts() == dict(input_tokens=38, output_tokens=11, cache_read_tokens=20, cache_write_tokens=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_worker_emits_usage_before_tool_and_error(worker, monkeypatch, success):
    import json
    async def empty(*args, **kwargs):
        return b""
    process = SimpleNamespace(stdout=None, stderr=SimpleNamespace(read=empty),
        stdin=SimpleNamespace(write=lambda _: None, drain=empty, close=lambda: None))
    async def spawn(*args, **kwargs):
        return process
    monkeypatch.setattr(worker.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(worker, "stop", empty)
    async def messages(self, stdout):
        yield {"type": "stream_event", "event": {"type": "message_start", "message": {
            "id": "message", "usage": {"input_tokens": 100, "output_tokens": 1}}}}
        yield {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 10}}}
        yield {"event": "client_tool", "tool": "lookup"}
        if not success:
            raise TimeoutError()
        yield {"type": "result", "subtype": "success", "usage": {"input_tokens": 80, "output_tokens": 8}}
    monkeypatch.setattr(worker.ToolBridge, "messages", messages)
    workspace = worker.ROOT / "usage"
    workspace.mkdir()
    response = await worker.turn(worker.Turn(model="claude-test", session_id=str(uuid4()),
        workspace=str(workspace), content=[{"type": "text", "text": "hi"}]))
    lines = [json.loads(line) async for line in response.body_iterator]
    usage = [line for line in lines if line.get("event") == "usage"]
    assert usage[-1]["input_tokens"] == 100 and usage[-1]["output_tokens"] == 10
    call = next(line for line in lines if line.get("event") == "client_tool")
    assert call["input_tokens"] == 100 and call["output_tokens"] == 10
    if success:
        assert lines[-1]["done"] and lines[-1]["input_tokens"] == 80
    else:
        assert "TimeoutError" in lines[-1]["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_cross_node_usage_survives_serialization_and_failure(monkeypatch, success):
    import json
    import httpx
    from codex_gateway import cluster
    from codex_gateway.audit import current_audit
    from codex_gateway.backend import BackendTarget, WorkerFailure
    target = BackendTarget("key:worker", "http://worker", "/workspace", provider="claude")
    event = BackendStreamEvent(thread_id="thread", input_tokens=80, output_tokens=8, done=success,
                               usage_accounting={"run_id": "remote-run", "final": success})
    async def handler(request):
        lines = [event.model_dump_json()]
        if not success:
            lines.append(json.dumps({"error": {"message": "timeout", "kind": "connection", "safe_to_retry": False}}))
        return httpx.Response(200, text="\n".join(lines))
    original = httpx.AsyncClient
    monkeypatch.setattr(cluster.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    audit = {}
    token = current_audit.set(audit)
    try:
        events = cluster.remote_stream("http://owner", ResponseRequest(model="claude-test", input="hi"), target, get_settings())
        if success:
            result = await cluster.collect(events)
            assert result.usage_accounting == event.usage_accounting
        else:
            with pytest.raises(WorkerFailure):
                await cluster.collect(events)
        assert audit["claude_usage"]["tokens"]["input_tokens"] == 80
        assert audit["claude_usage"]["run_id"] == "remote-run"
        assert audit["claude_usage"]["final"] is success
    finally:
        current_audit.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_tool_boundaries_and_authoritative_result(success):
    accounting = {"run_id": "run", "final": False}
    target = SimpleNamespace(connection_key="key:worker", provider="claude")
    async def events(request, target, run):
        yield BackendStreamEvent(thread_id="thread", input_tokens=100, output_tokens=10, usage_accounting=accounting)
        await sessions.await_result(run, {"call_id": "call"})
        yield BackendStreamEvent(thread_id="thread", tool_call={"call_id": "call"},
                                 input_tokens=100, output_tokens=12, usage_accounting=accounting)
        await sessions.receive_result(run)
        yield BackendStreamEvent(thread_id="thread", input_tokens=150, output_tokens=20, usage_accounting=accounting)
        if not success:
            raise RuntimeError("timeout")
        yield BackendStreamEvent(thread_id="thread", done=True, input_tokens=80, output_tokens=0,
                                 usage_accounting={**accounting, "final": True})
    sessions = ToolSessions(events)
    first = [e async for e in sessions.stream(ResponseRequest(model="model", input="hi"), target)]
    assert (first[-1].input_tokens, first[-1].output_tokens) == (100, 12)
    followup = ResponseRequest(model="model", input=[{"type": "function_call_output", "call_id": "call", "output": "ok"}])
    second = []
    try:
        async for event in sessions.stream(followup, target):
            second.append(event)
    except RuntimeError:
        assert not success
    assert (second[0].input_tokens, second[0].output_tokens) == (50, 8)
    if success:
        assert (second[-1].input_tokens, second[-1].output_tokens) == (80, 0)
    await sessions.close()


@pytest_asyncio.fixture
async def database():
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    model = "accounting-" + uuid4().hex
    async with factory() as db:
        db.add(ModelPrice(model=model, input_price=2, output_price=8, cache_read_price=1, cache_write_price=3))
        await db.commit()
    yield factory, model
    await engine.dispose()


def observed(run, count, final=False):
    return {"run_id": run, "final": final, "tokens": dict(zip(TOKEN_FIELDS, count))}


async def write(factory, model, usage, status=200):
    record = UsageRecord(request_id="resp_" + uuid4().hex, model=model, provider="claude",
                         status_code=status, thread_id="same-thread", conversation_evidence={})
    async with factory() as db:
        await apply_usage(db, record, usage)
        db.add(record)
        await db.commit()
    return record.request_id


@pytest.mark.asyncio
async def test_success_replaces_only_this_run_and_late_increments(database):
    factory, model = database
    run = uuid4().hex
    first = await write(factory, model, observed(run, (100, 20, 30, 10)))
    # Same thread, different CLI execution must remain billable.
    other = await write(factory, model, observed(uuid4().hex, (200, 30, 0, 0)))
    final = await write(factory, model, observed(run, (80, 0, 20, 10), True))
    late = await write(factory, model, observed(run, (60, 8, 10, 0)))
    async with factory() as db:
        rows = {r.request_id: r for r in (await db.scalars(select(UsageRecord).where(UsageRecord.model == model))).all()}
    for key in (first, late):
        assert rows[key].cost_usd == 0
        assert all(getattr(rows[key], field) == 0 for field in TOKEN_FIELDS)
        assert rows[key].conversation_evidence["usage_accounting"]["superseded_by"] == final
    assert rows[first].conversation_evidence["usage_accounting"]["tokens"]["input_tokens"] == 100
    assert rows[other].input_tokens == 200
    assert rows[final].input_tokens == 80 and rows[final].output_tokens == 0
    assert rows[final].cost_usd == Decimal("0.000150")


@pytest.mark.asyncio
async def test_failure_keeps_incremental_usage_and_cost(database):
    factory, model = database
    run = uuid4().hex
    await write(factory, model, observed(run, (100, 20, 30, 10)))
    await write(factory, model, observed(run, (50, 5, 10, 0)), status=502)
    async with factory() as db:
        rows = (await db.scalars(select(UsageRecord).where(UsageRecord.model == model))).all()
    assert sum(row.input_tokens for row in rows) == 150
    assert sum(row.output_tokens for row in rows) == 25
    assert sum(row.cost_usd for row in rows) == Decimal("0.000470")


@pytest.mark.asyncio
async def test_concurrent_nodes_settle_once(database):
    factory, model = database
    run = uuid4().hex
    await asyncio.gather(
        write(factory, model, observed(run, (100, 20, 0, 0))),
        write(factory, model, observed(run, (80, 10, 0, 0), True)),
        write(factory, model, observed(run, (30, 10, 0, 0))),
    )
    async with factory() as db:
        rows = (await db.scalars(select(UsageRecord).where(UsageRecord.model == model))).all()
    assert sum(row.input_tokens for row in rows) == 80
    assert sum(row.output_tokens for row in rows) == 10
    assert sum(row.cost_usd for row in rows) == Decimal("0.000240")


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [True, False])
async def test_audit_fallback_persists_known_usage(database, monkeypatch, interrupted):
    import json
    from codex_gateway import audit
    from codex_gateway.auth import ApiPrincipal
    from codex_gateway.backend import BackendTarget
    from codex_gateway.usage_accounting import observe_usage
    factory, model = database
    monkeypatch.setattr(audit, "SessionLocal", factory)
    monkeypatch.setattr(get_settings(), "model_providers", model + ":claude")
    target = BackendTarget("key:worker", "http://worker", "/workspace", provider="claude")
    async def app(scope, receive, send):
        await receive()
        audit.current_audit.get()["principal"] = ApiPrincipal(None, "test")
        observe_usage(target, BackendStreamEvent(thread_id="thread", input_tokens=100, output_tokens=20,
            usage_accounting={"run_id": uuid4().hex, "final": False}))
        if interrupted:
            raise asyncio.CancelledError()
        await send({"type": "http.response.start", "status": 502})
        await send({"type": "http.response.body", "body": b"failed"})
    async def receive():
        return {"type": "http.request", "body": json.dumps({"model": model, "input": "hi"}).encode()}
    async def send(message):
        pass
    scope = {"type": "http", "method": "POST", "path": "/v1/responses", "headers": [], "state": {}}
    try:
        await audit.RequestAuditMiddleware(app)(scope, receive, send)
    except asyncio.CancelledError:
        assert interrupted
    async with factory() as db:
        record = await db.scalar(select(UsageRecord).where(UsageRecord.model == model))
    assert record is not None
    assert record.status_code == (499 if interrupted else 502)
    assert (record.input_tokens, record.output_tokens, record.cost_usd) == (100, 20, Decimal("0.00036"))
    assert record.thread_id == "thread"


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [False, True])
@pytest.mark.parametrize("success", [False, True])
async def test_stream_persistence_and_final_zero_correction(database, monkeypatch, chat, success):
    import json
    from codex_gateway import main
    from codex_gateway.audit import current_audit
    from codex_gateway.auth import ApiPrincipal
    from codex_gateway.backend import BackendTarget, WorkerFailure
    from codex_gateway.schemas import ChatCompletionRequest
    from codex_gateway.usage_accounting import observe_usage
    factory, model = database
    monkeypatch.setattr(main, "SessionLocal", factory)
    target = BackendTarget("key:worker", "http://worker", "/workspace", provider="claude")
    run = uuid4().hex
    await write(factory, model, observed(run, (100, 20, 0, 0)))
    class Backend:
        async def stream(self, *args):
            partial = BackendStreamEvent(thread_id="same-thread", input_tokens=50, output_tokens=5,
                                         usage_accounting={"run_id": run, "final": False})
            observe_usage(target, partial)
            yield partial
            if not success:
                raise WorkerFailure("timeout")
            final = BackendStreamEvent(thread_id="same-thread", done=True, input_tokens=80, output_tokens=0,
                                       usage_accounting={"run_id": run, "final": True})
            observe_usage(target, final)
            yield final
    body = ResponseRequest(model=model, input="hi")
    import hashlib
    raw = json.dumps(body.model_dump(mode="json")).encode()
    token = current_audit.set({"body": raw, "transport": {}, "body_bytes_received": len(raw),
                               "body_hash": hashlib.sha256(raw), "body_complete": True})
    try:
        principal = ApiPrincipal(None, "test")
        if chat:
            chat_body = ChatCompletionRequest(model=model, messages=[{"role": "user", "content": "hi"}])
            events = main.chat_completion_stream(chat_body, body, Backend(), principal, target, allow_retry=False)
        else:
            events = main.response_stream(body, Backend(), principal, target, None, allow_retry=False)
        payloads = [p async for p in events]
        assert payloads
    finally:
        current_audit.reset(token)
    async with factory() as db:
        rows = (await db.scalars(select(UsageRecord).where(UsageRecord.model == model))).all()
    assert len(rows) == 2
    assert sum(row.input_tokens for row in rows) == (80 if success else 150)
    assert sum(row.output_tokens for row in rows) == (0 if success else 25)
    assert sorted(row.status_code for row in rows) == ([200, 200] if success else [200, 502])
