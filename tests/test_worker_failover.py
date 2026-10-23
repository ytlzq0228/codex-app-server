from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from codex_gateway import main
from codex_gateway.auth import ApiPrincipal
from codex_gateway.backend import AppServerBackend, BackendTarget, WorkerFailure, classify_worker_failure
from codex_gateway.config import Settings
from codex_gateway.schemas import BackendResult, BackendStreamEvent, ResponseRequest


@pytest.mark.parametrize('message,kind', [
    ('Your workspace is out of credits', 'limit'),
    ('Selected model is at capacity', 'capacity'),
    ('429 Too Many Requests', 'limit'),
])
def test_failure_classification(message, kind):
    assert classify_worker_failure(message) == kind


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['codex', 'claude', 'gemini'])
async def test_auto_resume_reselects_after_quota_rejection(monkeypatch, provider):
    monkeypatch.setattr(main, 'provider_for', lambda model: provider)
    target = BackendTarget('new', 'http://worker', '/workspace', uuid4(), provider=provider)
    choose = AsyncMock(side_effect=[HTTPException(503), target])
    monkeypatch.setattr(main, 'choose_target', choose)
    body = ResponseRequest(model='test', input='full history', previous_response_id='old')
    body._execution_auto_resume = True
    body._execution_input_text = 'delta'
    body._execution_input_items = [{'role': 'user', 'content': 'delta'}]
    assert await main.choose_execution_target(ApiPrincipal(None, 'test'), None, SimpleNamespace(worker_id=uuid4()), body) == target
    assert body.previous_response_id is None
    assert body._execution_input_items is None and body._execution_input_text is None
    assert choose.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['codex', 'claude', 'gemini'])
async def test_quota_error_with_status_can_fail_over(monkeypatch, provider):
    first = BackendTarget('old', 'http://old', '/workspace', uuid4(), provider=provider)
    second = BackendTarget('new', 'http://new', '/workspace', uuid4(), provider=provider)
    backend = SimpleNamespace(complete=AsyncMock(side_effect=[
        WorkerFailure('quota exhausted', kind='limit', safe_to_retry=True,
                      code='provider_quota_exhausted', status=429),
        BackendResult(text='ok', thread_id='new-thread')]))
    quarantine = AsyncMock()
    monkeypatch.setattr(main, 'quarantine_worker', quarantine)
    monkeypatch.setattr(main, 'retry_target', AsyncMock(return_value=second))
    body = ResponseRequest(model='test', input='full history', previous_response_id='old-thread')
    body._execution_auto_resume = True
    result, selected = await main.complete_with_failover(body, backend, ApiPrincipal(None, 'test'), first, allow_retry=True)
    assert result.text == 'ok' and selected == second
    assert body.previous_response_id is None
    quarantine.assert_awaited_once_with(first.worker_id, 'quota exhausted', 'limit')


@pytest.mark.asyncio
@pytest.mark.parametrize('used', [79.99, 80, 100])
async def test_live_quota_checked_before_resuming_thread(used):
    calls = []
    class Server:
        async def call(self, method, params):
            calls.append(method)
            if method == 'account/read':
                return {'account': {'type': 'chatgpt'}}
            if method == 'account/rateLimits/read':
                return {'rateLimits': {'primary': {'windowDurationMins': 300, 'usedPercent': used}}}
            if method == 'thread/resume':
                return {'thread': {'id': 'old'}}
            if method == 'turn/start':
                return {'turn': {'id': 'turn'}}
            return {}
        async def messages(self):
            yield {'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}
    class Pool:
        @asynccontextmanager
        async def lease(self, *args):
            yield Server(), 0
    backend = AppServerBackend(Settings())
    backend.pool = Pool()
    body = ResponseRequest(model='test', input='hello', previous_response_id='old')
    target = BackendTarget('key', 'http://worker', '/workspace')
    if used >= 80:
        with pytest.raises(WorkerFailure) as caught:
            await backend.complete(body, target)
        assert caught.value.safe_to_retry and caught.value.kind == 'limit'
        assert 'thread/resume' not in calls and 'turn/start' not in calls
    else:
        await backend.complete(body, target)
        assert 'turn/start' in calls


@pytest.mark.asyncio
@pytest.mark.parametrize('effect', [None, 'item/agentMessage/delta', 'item/started'])
async def test_terminal_quota_retry_requires_no_prior_effects(effect):
    class Server:
        async def call(self, method, params):
            if method == 'account/read':
                return {'account': {'type': 'apiKey'}}
            if method == 'thread/start':
                return {'thread': {'id': 'thread'}}
            return {'turn': {'id': 'turn'}}
        async def messages(self):
            if effect:
                yield {'method': effect, 'params': {'delta': 'partial', 'item': {'type': 'commandExecution'}}}
            yield {'method': 'turn/completed', 'params': {'turn': {'status': 'failed', 'error': {'message': 'out of credits'}}}}
    class Pool:
        @asynccontextmanager
        async def lease(self, *args):
            yield Server(), 0
    backend = AppServerBackend(Settings())
    backend.pool = Pool()
    with pytest.raises(WorkerFailure) as caught:
        await backend.complete(ResponseRequest(model='test', input='hello'), BackendTarget('key', 'http://worker', '/workspace'))
    assert caught.value.safe_to_retry is (effect is None)
    assert caught.value.status == 429


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_failover_excludes_all_failed_workers(monkeypatch, stream):
    targets = [BackendTarget(str(i), 'http://worker', '/workspace', uuid4()) for i in range(3)]
    calls = []
    async def replacement(principal, failed, model, *, exclude_worker_ids):
        assert failed.worker_id in exclude_worker_ids
        return next(t for t in targets if t.worker_id not in exclude_worker_ids)
    class Backend:
        async def complete(self, body, target):
            calls.append(target.worker_id)
            if target != targets[-1]:
                raise WorkerFailure('quota', kind='limit', safe_to_retry=True, code='provider_quota_exhausted', status=429)
            return BackendResult(text='ok', thread_id='new')
        async def stream(self, body, target):
            result = await self.complete(body, target)
            yield BackendStreamEvent(delta=result.text, thread_id=result.thread_id, done=True)
    monkeypatch.setattr(main, 'retry_target', replacement)
    monkeypatch.setattr(main, 'quarantine_worker', AsyncMock())
    monkeypatch.setattr(main, 'save_usage', AsyncMock())
    request = ResponseRequest(model='test', input='full history')
    principal = ApiPrincipal(None, 'test')
    if stream:
        events = [e async for e in main.response_stream(request, Backend(), principal, targets[0], None)]
        assert 'response.completed' in events[-1]
    else:
        result, selected = await main.complete_with_failover(request, Backend(), principal, targets[0], allow_retry=True)
        assert result.text == 'ok' and selected == targets[-1]
    assert calls == [t.worker_id for t in targets]


@pytest.mark.asyncio
async def test_stream_never_replays_after_tool_call(monkeypatch):
    class Backend:
        async def stream(self, body, target):
            yield BackendStreamEvent(thread_id='old', tool_call={'type': 'function_call', 'call_id': 'call', 'name': 'write', 'arguments': '{}'})
            raise WorkerFailure('quota', kind='limit', safe_to_retry=True)
    retry = AsyncMock()
    monkeypatch.setattr(main, 'retry_target', retry)
    monkeypatch.setattr(main, 'quarantine_worker', AsyncMock())
    monkeypatch.setattr(main, 'save_usage', AsyncMock())
    events = [e async for e in main.response_stream(ResponseRequest(model='test', input='hello'), Backend(),
              ApiPrincipal(None, 'test'), BackendTarget('old', 'http://worker', '/workspace', uuid4()), None)]
    retry.assert_not_awaited()
    assert 'response.failed' in events[-1]


def test_worker_toggle_does_not_hold_row_while_waiting_for_quota(monkeypatch):
    import asyncio
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from codex_gateway import admin
    from codex_gateway.database import SessionLocal
    from codex_gateway.models import Worker, WorkerStatus
    from codex_gateway.quota import quota_lock
    monkeypatch.setattr(admin, 'verify_csrf', lambda *args: None)
    async def run():
        async with SessionLocal() as db:
            worker = Worker(name='lock-' + uuid4().hex, container_name='lock-' + uuid4().hex,
                            endpoint='http://worker', status=WorkerStatus.ready, enabled=True)
            db.add(worker)
            await db.commit()
            worker_id = worker.id
        entered = asyncio.Event()
        async def toggle():
            async with SessionLocal() as db:
                entered.set()
                await admin.toggle_worker_state(None, worker_id, 'csrf', None, db)
        async with SessionLocal() as owner:
            await quota_lock(owner)
            task = asyncio.create_task(toggle())
            try:
                await entered.wait()
                await asyncio.sleep(.05)
                # A waiter must not hold the Worker row needed by the quota owner.
                row = await asyncio.wait_for(owner.scalar(select(Worker).where(Worker.id == worker_id).with_for_update()), 2)
                row.last_seen_at = main.utcnow()
                await owner.commit()
                await asyncio.wait_for(task, 3)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        async with SessionLocal() as db:
            worker = await db.get(Worker, worker_id)
            assert worker.enabled is False
            await db.delete(worker)
            await db.commit()
    with TestClient(main.app) as client:
        client.portal.call(run)


@pytest.mark.asyncio
async def test_pending_tool_survives_quota_reserve(monkeypatch):
    from codex_gateway.client_tools import ToolProtocolError
    target = BackendTarget('key:worker', 'http://worker', '/workspace', uuid4(), 1)
    worker = SimpleNamespace(endpoint=target.endpoint, execution_generation=1, enabled=True, status='ready', id=target.worker_id)
    sessions = SimpleNamespace(runs=[SimpleNamespace(target=target, thread_id='thread')], cancel_thread=AsyncMock())
    monkeypatch.setattr(main, 'pool_routing_signals', AsyncMock(return_value=({}, {
        'workers': {str(worker.id): {'week_used': 80}}}, set())))
    await main.validate_pending_worker(SimpleNamespace(tool_sessions=sessions), target, SimpleNamespace(get=AsyncMock(return_value=worker)))
    sessions.cancel_thread.assert_not_awaited()



@pytest.mark.asyncio
async def test_codex_quota_failure_enters_cooldown_without_probe(monkeypatch):
    worker = SimpleNamespace(provider='codex')
    session = SimpleNamespace(scalar=AsyncMock(return_value=worker), commit=AsyncMock())
    @asynccontextmanager
    async def database():
        yield session
    probe = AsyncMock(return_value={'ok': True})
    monkeypatch.setattr(main, 'SessionLocal', database)
    monkeypatch.setattr(main, 'probe_worker_record', probe)
    monkeypatch.setattr(main, 'reconcile_worker', AsyncMock())
    monkeypatch.setattr('codex_gateway.quota.quota_lock', AsyncMock())
    assert await main.quarantine_worker(uuid4(), 'out of credits', 'limit')
    assert worker.status == 'error' and worker.failure_kind == 'limit'
    assert worker.retry_after > worker.quarantined_at
    probe.assert_not_awaited()
    session.commit.assert_awaited_once()


def test_explicit_full_history_can_rebuild_on_another_worker(monkeypatch):
    import hashlib
    import json
    from fastapi.testclient import TestClient
    from codex_gateway import execution
    from codex_gateway.database import SessionLocal
    from codex_gateway.models import ApiKey, Worker, ResponseBinding
    conversation = str(uuid4())
    def audit(body):
        return {'body': json.dumps({**body.model_dump(mode='json'), 'client_metadata': {'thread_id': conversation}}).encode(),
                'transport': {'headers': []}, 'body_hash': hashlib.sha256(), 'body_bytes_received': 1, 'body_complete': True}
    async def run():
        async with SessionLocal() as db:
            key = ApiKey(name='full-history', prefix=uuid4().hex[:20], key_hash=uuid4().hex * 2)
            worker = Worker(name='history-' + uuid4().hex, container_name='history-' + uuid4().hex, endpoint='http://worker')
            db.add_all([key, worker])
            await db.commit()
        principal = ApiPrincipal(key.id, 'test')
        target = BackendTarget('key:old', worker.endpoint, '/workspace', worker.id)
        first = ResponseRequest(model='gpt-6-sol', input=[{'role': 'user', 'content': 'first'}])
        first_audit = audit(first)
        await execution.prepare(first, principal, 'responses', first_audit)
        async with SessionLocal() as db:
            binding = ResponseBinding(response_id='resp_' + uuid4().hex, api_key_id=key.id, worker_id=worker.id, thread_id='old-thread')
            db.add(binding)
            await execution.finish(db, first_audit, BackendResult(text='answer', thread_id='old-thread'), target, binding.response_id)
            await db.commit()
        await execution.cleanup(first_audit)
        follow = ResponseRequest(model='gpt-6-sol', previous_response_id='old-thread', input=[
            *first.input, {'role': 'assistant', 'content': 'answer'}, {'role': 'user', 'content': 'next'}])
        follow_audit = audit(follow)
        prepared, chosen = await execution.prepare(follow, principal, 'responses', follow_audit, binding=binding)
        try:
            assert prepared._execution_auto_resume
            assert prepared._execution_input_items == [follow.input[-1]]
            new = BackendTarget('key:new', 'http://new', '/workspace', uuid4())
            monkeypatch.setattr(main, 'choose_target', AsyncMock(side_effect=[HTTPException(503), new]))
            assert await main.choose_execution_target(principal, None, chosen, prepared) == new
            assert prepared.previous_response_id is None
            assert prepared._execution_input_items is None
            assert prepared.input == follow.input
        finally:
            await execution.cleanup(follow_audit)
    with TestClient(main.app) as client:
        client.portal.call(run)
