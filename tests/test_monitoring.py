from page_helpers import rendered_pages
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from codex_gateway.database import SessionLocal
from codex_gateway.main import app
from codex_gateway.models import MetricSnapshot, SubscriptionPlan
from codex_gateway.monitoring import pool_usage, record_snapshot, state_counts, worker_state
from test_self_service import admin_login, AJAX, user_login
from test_quota_workers import create_person


def worker(**kwargs):
    return SimpleNamespace(**dict(dict(id=uuid4(), endpoint='ws://test', auth_mode='chatgpt',
        failure_kind=None, status='ready', enabled=True, plan_type='plus'), **kwargs))


def test_exhaustive_worker_states():
    examples = [(worker(), 'healthy'), (worker(status='busy'), 'healthy'),
        (worker(status='error', failure_kind='limit'), 'limited'),
        (worker(status='error', failure_kind='connection'), 'service'),
        (worker(auth_mode=None), 'logged_out'),
        (worker(failure_kind='logged_out'), 'logged_out'),
        (worker(endpoint='removed://worker'), 'other'),
        (worker(enabled=False), 'other'), (worker(status='draining'), 'other'),
        (worker(status='error', failure_kind='unknown'), 'other')]
    for item, expected in examples:
        assert worker_state(item) == expected
    counts = state_counts([w for w, _ in examples])
    assert counts['total'] == sum(counts['counts'].values()) == 9


def test_weighted_windows_missing_data_and_risk():
    a, b, missing = worker(), worker(plan_type='pro'), worker(plan_type=None)
    readings = {
        str(a.id): {'buckets': [{'five_hour': {'used': 10}, 'week': {'used': 80}}]},
        str(b.id): {'buckets': [{'five_hour': {'used': 50}}, {'five_hour': {'used': 70}}]},
    }
    data = pool_usage([a,b,missing,worker(auth_mode=None),worker(endpoint='removed://worker')],
                      {'plus': 1, 'pro': 3}, readings)
    assert data['eligible'] == 3 and data['total_weight'] == 5 and data['unknown_plans'] == 1
    assert data['windows']['risk'] == {'used': 72.5, 'covered_weight': 4, 'covered': 2}
    assert data['windows']['five_hour']['used'] == 55
    assert data['windows']['week']['used'] == 20
    assert data['workers'][str(a.id)]['weighted_remaining'] == 20
    assert data['workers'][str(a.id)]['five_hour_used'] == 10
    assert data['workers'][str(b.id)]['five_hour_used'] == 70
    assert data['workers'][str(b.id)]['weighted_remaining'] == 300
    assert data['workers'][str(missing.id)]['weighted_remaining'] is None
    assert pool_usage([a], {}, {})['windows']['risk']['used'] is None
    assert pool_usage([], {}, {})['windows']['risk']['used'] is None


def test_snapshots_api_weights_and_history(monkeypatch):
    import codex_gateway.monitoring as monitoring
    async def empty_read(worker, semaphore):
        return str(worker.id), {}
    monkeypatch.setattr(monitoring, 'read_usage', empty_read)
    now = datetime.now(timezone.utc) - timedelta(days=2)
    async def sample():
        for metric, minutes in [('worker_states', 10), ('subscription_usage', 60)]:
            bucket = now.replace(minute=now.minute//minutes*minutes, second=0, microsecond=0)
            async with SessionLocal() as db:
                await db.execute(delete(MetricSnapshot).where(MetricSnapshot.metric == metric, MetricSnapshot.bucket_at == bucket))
                await db.commit()
            await asyncio.gather(record_snapshot(metric, minutes, now), record_snapshot(metric, minutes, now))
            await record_snapshot(metric, minutes, now)
            async with SessionLocal() as db:
                rows = (await db.scalars(select(MetricSnapshot).where(MetricSnapshot.metric == metric, MetricSnapshot.bucket_at == bucket))).all()
                assert len(rows) == 1
                assert rows[0].payload['version'] == (2 if metric == 'worker_states' else 5)
    with TestClient(app) as client:
        assert rendered_pages(client, '/admin/monitoring', headers=AJAX).status_code in (401, 303)
        token = admin_login(client)
        client.portal.call(sample)
        page = rendered_pages(client, '/admin').text
        assert 'id="monitoring"' in page and 'id="keys"' not in page and 'id="workers"' not in page
        response = rendered_pages(client, '/admin/monitoring?days=7')
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        assert any(r['at'].startswith(now.strftime('%Y-%m-%d')) for r in response.json()['history']['worker_states'])
        assert not any(r['at'].startswith(now.strftime('%Y-%m-%d')) for r in rendered_pages(client, '/admin/monitoring?days=1').json()['history']['worker_states'])
        assert rendered_pages(client, '/admin/monitoring?days=100').status_code == 400
        summary = client.get('/admin/monitoring?view=summary').json()
        assert 'history' not in summary and 'states' in summary
        history = client.get('/admin/monitoring?view=history&days=7', headers={'Accept-Encoding': 'gzip'})
        assert history.headers['content-encoding'] == 'gzip'
        data = history.json()
        assert 'states' not in data and data['intervals']['subscription_usage'] == 1800000
        import json
        assert '"workers"' not in json.dumps(data)
        assert '"weights"' not in json.dumps(summary)
        delta = client.get('/admin/monitoring', params={'view':'history', 'since':data['server_time']}).json()
        assert all(r['at'] >= delta['replace_from']['subscription_usage'] for r in delta['history']['subscription_usage'])
        assert client.get('/admin/monitoring?view=invalid').status_code == 400
        assert client.get('/admin/monitoring?since=2026-01-01T00:00:00').status_code == 400

        plan = 'monitor-' + uuid4().hex
        for weight in ['0', '-1', 'NaN', 'Infinity', '0.0000001', '1000000000']:
            assert client.post('/admin/subscription-plans', data={'csrf_token':token,'name':plan,'monthly_price':'20','weight':weight},headers=AJAX).status_code in (400,422)
        assert client.post('/admin/subscription-plans',data={'csrf_token':token,'name':plan,'monthly_price':'20','weight':'2.5'},headers=AJAX).status_code == 200
        async def saved():
            async with SessionLocal() as db:
                assert float((await db.get(SubscriptionPlan,plan)).weight) == 2.5
        client.portal.call(saved)
        name, pw = create_person(client)
        user_login(client,name,pw)
        assert rendered_pages(client, '/admin/monitoring',headers=AJAX).status_code == 403


@pytest.mark.asyncio
async def test_usage_poll_is_read_only_and_failure_is_missing(monkeypatch):
    import codex_gateway.monitoring as monitoring
    calls = []
    class Server:
        async def call(self, method, params):
            calls.append(method)
            return {'rateLimits': {'primary': {'usedPercent': 25, 'windowDurationMins': 300}}}
    @asynccontextmanager
    async def opened(*args, **kwargs):
        yield Server()
    monkeypatch.setattr(monitoring, 'open_app_server', opened)
    item = worker(status='error', failure_kind='limit')
    ident, data = await monitoring.read_usage(item, asyncio.Semaphore(1))
    assert ident == str(item.id) and data['buckets'][0]['five_hour']['used'] == 25
    assert calls == ['account/rateLimits/read'] and item.failure_kind == 'limit'
    @asynccontextmanager
    async def failed(*args, **kwargs):
        raise RuntimeError('unreachable')
        yield
    monkeypatch.setattr(monitoring, 'open_app_server', failed)
    assert await monitoring.read_usage(item, asyncio.Semaphore(1)) == (str(item.id), {})


def test_successful_missing_windows_are_unlimited_but_failure_is_unknown():
    a, b, failed = worker(), worker(plan_type='pro'), worker()
    readings = {str(a.id): {'buckets': []}, str(b.id): {'buckets': [{'five_hour': None, 'week': None}]}, str(failed.id): {}}
    result = pool_usage([a,b,failed], {'plus': 1, 'pro': 3}, readings)
    assert result['version'] == 5
    for window in result['windows'].values():
        assert window == {'used': 0, 'covered_weight': 4, 'covered': 2}
    assert result['total_weight'] == 5


def test_removed_workers_are_excluded_from_state_total_and_percentages():
    active = [worker() for _ in range(4)]
    counts = state_counts(active + [worker(endpoint='removed://worker', enabled=False, auth_mode=None)])
    assert counts['version'] == 2
    assert counts['total'] == 4
    assert counts['counts']['healthy'] == 4
    assert counts['counts']['other'] == 0
    assert counts['counts']['healthy'] / counts['total'] == 1
    assert state_counts([worker(endpoint='removed://worker')])['total'] == 0
    # Disabled workers that still exist remain visible as abnormal.
    assert state_counts([worker(enabled=False)])['counts']['other'] == 1


def test_provider_pools_have_independent_weights_and_coverage():
    codex = worker(provider='codex', plan_type='plus')
    gemini = worker(provider='gemini', plan_type='enterprise')
    failed = worker(provider='gemini', plan_type='enterprise')
    result = pool_usage([codex, gemini, failed], {'plus': 1, 'gemini:enterprise': 10}, {
        str(codex.id): {'buckets': [{'five_hour': {'used': 80}, 'week': {'used': 40}}]},
        str(gemini.id): {'buckets': [], 'unlimited': True},
    })
    c, g = result['providers']['codex'], result['providers']['gemini']
    assert c['windows']['risk']['used'] == 80
    assert c['total_weight'] == 1 and c['eligible'] == 1
    assert g['total_weight'] == 20 and g['eligible'] == 2
    assert g['windows']['week'] == {'used': 0, 'covered_weight': 10, 'covered': 1}
    assert result['workers'][str(failed.id)]['weighted_remaining'] is None
    # A textual numeric quota is not a confirmed unlimited response.
    unknown = pool_usage([gemini], {}, {str(gemini.id): {'buckets': [], 'available': True, 'message': '50%'}})
    assert unknown['providers']['gemini']['windows']['risk']['used'] is None


@pytest.mark.asyncio
async def test_gemini_monitor_reads_enterprise_quota_and_keeps_errors_unknown(monkeypatch):
    import codex_gateway.gemini_backend as backend
    from codex_gateway.monitoring import read_usage
    item = worker(provider='gemini')
    async def success(endpoint, settings, path):
        assert path == '/rate-limits'
        return {'available': False, 'buckets': [], 'message': 'enterprise'}
    monkeypatch.setattr(backend, 'worker_rpc', success)
    _, reading = await read_usage(item, asyncio.Semaphore(1))
    assert reading['unlimited'] is True and reading['message'] == '无限制'
    async def fail(*args):
        raise RuntimeError('failed')
    monkeypatch.setattr(backend, 'worker_rpc', fail)
    assert await read_usage(item, asyncio.Semaphore(1)) == (str(item.id), {})


def test_history_aggregation_keeps_extrema_and_missing_samples():
    from codex_gateway.monitoring import aggregate_history
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    def sample(minutes, used):
        return SimpleNamespace(bucket_at=at+timedelta(minutes=minutes), observed_at=at+timedelta(minutes=minutes),
            payload={'providers': {'codex': {'windows': {'risk': {'used': used}}}}})
    rows = aggregate_history([sample(0, 10), sample(5, 100), sample(10, None), sample(60, None)], 1800, 'subscription_usage')
    assert len(rows) == 2  # No invented point in the missing 00:30 bucket.
    assert rows[0]['data']['providers']['codex']['windows']['risk'] == {
        'used': 55, 'min': 10, 'max': 100, 'samples': 2}
    assert rows[1]['data']['providers']['codex']['windows']['risk']['used'] is None
    assert rows[0]['data']['providers']['gemini']['windows']['risk']['used'] is None


def test_state_aggregation_uses_last_and_keeps_peak():
    from codex_gateway.monitoring import aggregate_history
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [SimpleNamespace(bucket_at=at+timedelta(minutes=i*10), observed_at=at+timedelta(minutes=i*10),
                            payload={'total': 10, 'counts': {'limited': value}})
            for i, value in enumerate([0, 8, 1])]
    data = aggregate_history(rows, 1800, 'worker_states')[0]['data']
    assert data['counts']['limited'] == 1
    assert data['max_counts']['limited'] == 8
