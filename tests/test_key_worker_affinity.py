from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import codex_gateway.main as main
from codex_gateway.auth import ApiPrincipal
from codex_gateway.models import UsageRecord, Worker, WorkerStatus


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', [
    'recent', 'expired', 'other_key', 'other_provider', 'failed', 'error',
    'disabled', 'offline', 'excluded', 'workspace_failure', 'pinned', 'bound',
    'no_key', 'latest', 'boundary', 'dead_node',
    'interrupted', 'interrupted_expired', 'interrupted_without_worker',
    'recent_saturated', 'all_saturated',
])
async def test_recent_key_worker_preference_and_fallback(monkeypatch, scenario):
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)

    class Clock:
        @staticmethod
        def now(_tz):
            return now

    monkeypatch.setattr(main, 'datetime', Clock)
    engine = create_engine('sqlite://')
    Worker.__table__.create(engine)
    UsageRecord.__table__.create(engine)
    with Session(engine) as db:
        preferred, fallback = [Worker(
            name=name, container_name=name, endpoint=f'ws://{name}',
            enabled=True, status=WorkerStatus.ready, provider='codex', node_id=name,
        ) for name in ('recent', 'fallback')]
        db.add_all([preferred, fallback])
        db.flush()
        key_id = uuid4()

        def usage(worker, **overrides):
            values = dict(request_id=str(uuid4()), api_key_id=key_id,
                          worker_id=worker.id, provider='codex', model='test',
                          status_code=200, created_at=now - timedelta(minutes=10))
            values.update(overrides)
            db.add(UsageRecord(**values))

        overrides = {
            'expired': dict(created_at=now - timedelta(hours=2, seconds=1)),
            'boundary': dict(created_at=now - timedelta(hours=2)),
            'other_key': dict(api_key_id=uuid4()),
            'other_provider': dict(provider='claude'),
            'failed': dict(status_code=503),
            'error': dict(error_code='backend_error'),
            'interrupted': dict(status_code=499, error_code='request_interrupted'),
            'interrupted_expired': dict(status_code=499, error_code='request_interrupted',
                                       created_at=now - timedelta(hours=2, seconds=1)),
            'interrupted_without_worker': dict(status_code=499, error_code='request_interrupted',
                                              worker_id=None),
        }.get(scenario, {})
        usage(preferred, **overrides)
        if scenario in {'latest', 'interrupted'}:
            usage(fallback, created_at=now - timedelta(minutes=20))
        if scenario == 'disabled':
            preferred.enabled = False
        if scenario == 'offline':
            preferred.status = WorkerStatus.offline
        db.commit()

        class AsyncSession:
            async def scalars(self, statement):
                return db.scalars(statement)

            async def scalar(self, statement):
                return db.scalar(statement)

            async def get(self, model, key):
                return db.get(model, key)

        async def signals(_session, _key):
            saturated = {str(preferred.id)} if scenario == 'recent_saturated' else (
                {str(preferred.id), str(fallback.id)} if scenario == 'all_saturated' else set())
            return {str(preferred.id): 5}, {'workers': {
                str(fallback.id): {'weighted_remaining': 100},
                str(preferred.id): {'weighted_remaining': 1},
            }}, saturated

        attempted, quarantined = [], []

        async def put(_client, url, **_kwargs):
            attempted.append(url)
            return httpx.Response(503 if scenario == 'workspace_failure' and
                                  '/workers/recent/' in url else 200)

        async def quarantine(worker_id, *_args):
            quarantined.append(worker_id)

        monkeypatch.setattr(main, 'pool_routing_signals', signals)
        monkeypatch.setattr(main.httpx.AsyncClient, 'put', put)
        monkeypatch.setattr(main, 'quarantine_worker', quarantine)
        if scenario == 'dead_node':
            import codex_gateway.cluster as cluster
            settings = main.get_settings().model_copy(update={'node_id': 'gateway'})
            monkeypatch.setattr(main, 'get_settings', lambda: settings)

            async def live_nodes(*_args):
                return [SimpleNamespace(id='fallback')]

            async def manager_for(*_args):
                return settings.manager_url

            monkeypatch.setattr(cluster, 'live_nodes', live_nodes)
            monkeypatch.setattr(cluster, 'manager_for', manager_for)
        principal = ApiPrincipal(None if scenario == 'no_key' else key_id, 'test',
                                 pinned_worker_id=fallback.id if scenario == 'pinned' else None)
        target = await main.choose_target(
            principal, AsyncSession(),
            binding=SimpleNamespace(worker_id=fallback.id) if scenario == 'bound' else None,
            exclude_worker_ids={preferred.id} if scenario == 'excluded' else None,
            cache_affinity='shared-prefix',
        )
        expected = preferred if scenario in {'recent', 'latest', 'boundary', 'interrupted', 'all_saturated'} else fallback
        assert target.worker_id == expected.id
        if scenario in {'pinned', 'bound', 'all_saturated', 'recent_saturated'}:
            assert not target.allow_capacity_fallback
        assert len(attempted) == (2 if scenario == 'workspace_failure' else 1)
        assert quarantined == ([preferred.id] if scenario == 'workspace_failure' else [])
    engine.dispose()
