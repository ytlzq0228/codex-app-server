import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from codex_gateway.app_server import AppServerSession
from codex_gateway.models import WorkerStatus


@pytest.mark.asyncio
async def test_rpc_notifications_cannot_extend_total_deadline():
    class Socket:
        async def send(self, message):
            pass

        async def recv(self):
            await asyncio.sleep(0.001)
            return json.dumps({'method': 'notification', 'params': {}})

    session = AppServerSession(Socket(), 0.02)
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(session.call('account/read'), 0.5)
    assert asyncio.get_running_loop().time() - started < 0.3
    assert session.pending_notifications


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', [False, True])
async def test_account_read_releases_db_and_rejects_stale_snapshot(monkeypatch, changed):
    from codex_gateway import contributions, database
    worker = SimpleNamespace(id=uuid4(), enabled=True, endpoint='ws://worker',
        execution_generation=1, account_checked_at=None, status=WorkerStatus.ready,
        provider='codex')
    state = {'open': False, 'locked': False}
    db = SimpleNamespace(get=AsyncMock(return_value=worker),
                         scalar=AsyncMock(return_value=worker), commit=AsyncMock())

    @asynccontextmanager
    async def session():
        state['open'] = True
        try:
            yield db
        finally:
            state['open'] = False

    async def lock(db):
        state['locked'] = True

    class Server:
        async def call(self, method, params):
            assert not state['open'] and not state['locked']
            if changed:
                worker.execution_generation += 1
            return {'account': {'type': 'chatgpt', 'planType': 'plus'}}

    @asynccontextmanager
    async def opened(*args):
        yield Server()

    update = AsyncMock()
    monkeypatch.setattr(database, 'SessionLocal', session)
    monkeypatch.setattr(contributions, 'quota_lock', lock)
    monkeypatch.setattr(contributions, 'open_app_server', opened)
    monkeypatch.setattr(contributions, 'update_account', update)
    monkeypatch.setattr(contributions, 'reconcile_worker', AsyncMock())
    await contributions.refresh_worker_account(worker.id)
    assert update.await_count == (0 if changed else 1)
    assert db.commit.await_count == (0 if changed else 1)


@pytest.mark.asyncio
async def test_quota_lock_wait_is_bounded_by_database():
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from codex_gateway.config import get_settings
    from codex_gateway.quota import quota_lock

    # Keep this async test's connections out of the TestClient lifespan pool.
    engine = create_async_engine(get_settings().database_url)
    sessions = async_sessionmaker(engine)
    try:
        async with sessions() as holder, sessions() as waiter:
            await holder.execute(text('SELECT pg_advisory_xact_lock(724193228)'))
            started = asyncio.get_running_loop().time()
            with pytest.raises(DBAPIError) as failure:
                await asyncio.wait_for(quota_lock(waiter), 8)
            assert getattr(failure.value.orig, 'sqlstate', None) == '55P03'
            assert asyncio.get_running_loop().time() - started < 7
            await waiter.rollback()
            await holder.rollback()
    finally:
        await engine.dispose()
