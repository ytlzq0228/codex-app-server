import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from codex_gateway import cluster
from codex_gateway.backend import BackendTarget, WorkerFailure
from codex_gateway.config import Settings
from codex_gateway.database import SessionLocal, engine
from codex_gateway.models import AppNode, Base, Worker, WorkerStatus
from codex_gateway.schemas import ResponseRequest


def test_concurrent_placement_is_balanced_and_dead_node_is_excluded():
    async def run():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        prefix = 'ha-' + uuid4().hex[:8]
        ids = [prefix + '-1', prefix + '-2']
        settings = Settings(node_id=ids[0], node_gateway_url='http://example', node_manager_url='http://example', bootstrap_worker=False)
        async with SessionLocal() as db:
            for node_id in ids:
                db.add(AppNode(id=node_id, gateway_url='http://example', manager_url='http://example',
                    heartbeat_at=datetime.now(timezone.utc), active_connections={}))
            await db.commit()
        async def create():
            async with SessionLocal() as db:
                node_id, _ = await cluster.select_node(db, settings)
                name = prefix + uuid4().hex
                db.add(Worker(name=name, container_name=name, endpoint='ws://worker',
                    node_id=node_id, status=WorkerStatus.offline))
                await asyncio.sleep(.01)
                await db.commit()
                return node_id
        try:
            chosen = await asyncio.gather(*(create() for _ in range(8)))
            assert [chosen.count(node_id) for node_id in ids] == [4, 4]
            async with SessionLocal() as db:
                stale = await db.get(AppNode, ids[1])
                stale.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=1)
                await db.commit()
                assert (await cluster.select_node(db, settings))[0] == ids[0]
                await db.rollback()
        finally:
            async with SessionLocal() as db:
                await db.execute(delete(Worker).where(Worker.node_id.in_(ids)))
                await db.execute(delete(AppNode).where(AppNode.id.in_(ids)))
                await db.commit()
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.asyncio
async def test_remote_stream_never_retries_ambiguous_disconnect(monkeypatch):
    @asynccontextmanager
    async def failing_stream(*args, **kwargs):
        raise httpx.ReadTimeout('turn may have started')
        yield
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        stream = staticmethod(failing_stream)
    monkeypatch.setattr(cluster.httpx, 'AsyncClient', lambda **kwargs: Client())
    target = BackendTarget('key:worker', 'ws://worker', '/workspace', uuid4())
    with pytest.raises(WorkerFailure) as failure:
        async for _ in cluster.remote_stream('http://owner', ResponseRequest(model='gpt-6-sol', input='hello'), target, Settings()):
            pass
    assert failure.value.safe_to_retry is False


@pytest.mark.asyncio
async def test_missing_terminal_event_is_an_error(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        async def aiter_lines(self):
            yield '{"delta":"partial","thread_id":"thread"}'
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        @asynccontextmanager
        async def stream(self, *args, **kwargs): yield Response()
    monkeypatch.setattr(cluster.httpx, 'AsyncClient', lambda **kwargs: Client())
    target = BackendTarget('key:worker', 'ws://worker', '/workspace', uuid4())
    with pytest.raises(WorkerFailure):
        async for _ in cluster.remote_stream('http://owner', ResponseRequest(model='gpt-6-sol', input='hello'), target, Settings()):
            pass


@pytest.mark.asyncio
async def test_legacy_manager_route_is_preserved():
    settings = Settings(node_id='', manager_url='http://legacy')
    assert await cluster.manager_for(None, None, settings) == 'http://legacy'
    assert await cluster.select_node(None, settings) == (None, 'http://legacy')


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["400", "409", "422", "500", "timeout", "malformed", "invalid_ack", "cancelled", "not_cancelled"])
async def test_supersede_preserves_owner_errors_and_requires_confirmation(monkeypatch, case):
    from unittest.mock import AsyncMock
    row = SimpleNamespace(api_key_id=uuid4(), thread_id="thread", history_hashes=["checkpoint", "call"],
                          logical_id="logical", response_id="response")
    body = ResponseRequest(model="gpt-6-astra", input=[
        {"role":"user", "content":"checkpoint"},
        {"type":"function_call", "call_id":"call", "name":"lookup", "arguments":"{}"},
        {"type":"function_call_output", "call_id":"call", "output":"done"},
        {"role":"user", "content":"Compact context"},
    ])
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(thread_id="thread", node_id="owner")),
                         scalar=AsyncMock(return_value=SimpleNamespace(gateway_url="http://owner")))
    monkeypatch.setattr(cluster, "get_settings", lambda: Settings(node_id="ingress", node_gateway_url="http://ingress", node_manager_url="http://manager", bootstrap_worker=False))
    error = {"message":"Cannot change tools", "code":"invalid_client_tool", "type":"invalid_request_error", "param":"tools"}
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            if case == "timeout": raise httpx.ReadTimeout("unknown outcome")
            status = int(case) if case.isdigit() else 200
            payload = {"error": error} if case.isdigit() else {"cancelled":case == "cancelled"}
            if case == "invalid_ack": payload = []
            if case == "malformed":
                return httpx.Response(400, text="not json", request=httpx.Request("POST", url))
            return httpx.Response(status, json=payload, request=httpx.Request("POST", url))
    monkeypatch.setattr(cluster.httpx, "AsyncClient", lambda **kwargs: Client())
    if case in {"cancelled", "not_cancelled"}:
        assert await cluster.supersede_tool(db, row, body, None) is (case == "cancelled")
    else:
        with pytest.raises(HTTPException) as caught:
            await cluster.supersede_tool(db, row, body, None)
        assert caught.value.status_code == (int(case) if case in {"400", "409", "422"} else 503)
        if case in {"400", "409", "422"}:
            assert caught.value.detail == {"error":error}
