from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from codex_gateway import prtg


def test_authentication_and_failure_do_not_leak(monkeypatch):
    monkeypatch.setattr(prtg, 'get_settings', lambda: SimpleNamespace(monitoring_token=SecretStr('x'*32), node_id='app-1'))
    app = FastAPI(); app.include_router(prtg.router)
    client = TestClient(app)
    assert client.get('/monitoring/prtg/workers').status_code == 401
    assert client.get('/monitoring/prtg/workers', headers={'Authorization':'Bearer wrong'}).status_code == 401
    monkeypatch.setattr(prtg, 'local_channels', AsyncMock(side_effect=RuntimeError('private database password')))
    response = client.get('/monitoring/prtg/workers', headers={'Authorization':'Bearer '+'x'*32})
    assert response.json()['prtg']['error'] == 1
    assert 'password' not in response.text
    assert response.headers['cache-control'] == 'no-store'


def test_quota_missing_stale_and_partial_coverage():
    p = {'total_weight':10, 'windows':{k:{'used':25, 'covered_weight':5} for k in ('five_hour','week','risk')}}
    rows = prtg.quota_channels(p, 300)
    assert rows[1]['value'] == 75 and rows[2]['value'] == 50
    assert rows[1]['limitminerror'] == 20
    assert rows[2]['limitminwarning'] == 50
    assert rows[2]['limitminerror'] == 20
    with pytest.raises(ValueError, match='older'):
        prtg.quota_channels(p, 901)
    p['windows']['week']['used'] = None
    with pytest.raises(ValueError, match='No valid'):
        prtg.quota_channels(p, 0)
    with pytest.raises(ValueError):
        prtg.channel('missing', float('nan'))


@pytest.mark.asyncio
async def test_throughput_owner_scope_and_tool_dedup():
    class DB:
        async def execute(self, query):
            from sqlalchemy.dialects import postgresql
            sql = str(query.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds':True}))
            assert "workers.node_id = 'app-2'" in sql
            assert 'JOIN workers ON workers.id = usage_records.worker_id' in sql
            return SimpleNamespace(all=lambda:[(60,120,200,['call1']), (0,0,500,['call1','call2'])])
    rows = await prtg.throughput(DB(), SimpleNamespace(node_id='app-2'), datetime.now(timezone.utc))
    assert rows[1]['value'] == 1
    assert rows[2]['value'] == 2
    assert rows[3]['value'] == 2/60


@pytest.mark.asyncio
async def test_gemini_counter_cleanup_on_close():
    from codex_gateway.gemini_backend import GeminiAdapter
    from codex_gateway.config import Settings
    from codex_gateway.backend import BackendTarget
    adapter = GeminiAdapter(Settings())
    async def events(*args):
        yield 'event'
        yield 'later'
    adapter._worker_events = events
    target = BackendTarget(connection_key='k:w', endpoint='http://test', workspace='/tmp', worker_id='w', provider='gemini')
    stream = adapter._turn_events(None, target)
    assert await anext(stream) == 'event'
    assert adapter.active_turns['w'] == 1
    await stream.aclose()
    assert not adapter.active_turns


def test_host_cpu_memory_and_network_deltas(monkeypatch):
    from codex_gateway import host_monitor
    readings = iter([
        {'cpu_total':100, 'cpu_idle':50, 'rx':100, 'tx':200, 'time':10, 'cpu_cores':8, 'memory_total_bytes':1000, 'memory_percent':25., 'disk_percent':60., 'load1':1.},
        {'cpu_total':200, 'cpu_idle':75, 'rx':300, 'tx':250, 'time':12, 'cpu_cores':8, 'memory_total_bytes':1000, 'memory_percent':25., 'disk_percent':60., 'load1':1.},
    ])
    monkeypatch.setattr(host_monitor, '_previous', None)
    monkeypatch.setattr(host_monitor, '_cached', None)
    monkeypatch.setattr(host_monitor, 'read_host', lambda *a: next(readings))
    monkeypatch.setattr(host_monitor.time, 'sleep', lambda _: None)
    got = host_monitor.snapshot()
    assert got['cpu_percent'] == 75
    assert got['rx_bytes_per_second'] == 100
    assert got['tx_bytes_per_second'] == 25
    assert got['memory_percent'] == 25


def test_missing_host_mount_never_reports_container_metrics(monkeypatch, tmp_path):
    from codex_gateway import host_monitor
    monkeypatch.setattr(host_monitor, '_previous', None)
    monkeypatch.setattr(host_monitor, '_cached', None)
    monkeypatch.setenv('CODEX_HOST_PROC', str(tmp_path/'missing'))
    with pytest.raises(FileNotFoundError):
        host_monitor.snapshot()
