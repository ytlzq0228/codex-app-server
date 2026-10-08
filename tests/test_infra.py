import asyncio

import httpx
from fastapi.testclient import TestClient

from codex_gateway.docker_monitor import usage
from codex_gateway.infra import probe_node
from codex_gateway.main import app
from test_self_service import admin_login


def test_docker_working_set_and_cpu():
    sample = {"cpu_stats": {"cpu_usage": {"total_usage": 400}, "system_cpu_usage": 1000, "online_cpus": 4},
              "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 400},
              "memory_stats": {"usage": 1000, "limit": 2000, "stats": {"inactive_file": 300}}}
    assert usage(sample)["cpu_percent"] == 200
    assert usage(sample)["memory_bytes"] == 700
    assert usage({})["cpu_percent"] is None
    assert usage({})["memory_bytes"] is None


def test_node_failure_preserves_other_status():
    def handler(request):
        if request.url.path == "/healthz":
            return httpx.Response(200)
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(503)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await probe_node(client, {"id": "app-2", "gateway_url": "http://app", "manager_url": "http://manager"}, "secret")
    result = asyncio.run(run())
    assert result["gateway_status"] == "online"
    assert result["manager_status"] == "offline"
    assert result["docker"] is None
    assert "manager_url" not in result


def test_infra_requires_admin():
    with TestClient(app) as client:
        assert client.get('/infra/', follow_redirects=False).status_code == 303
        assert client.get('/infra/status', follow_redirects=False).status_code == 303
        admin_login(client)
        response = client.get('/infra/')
        assert response.status_code == 200
        assert 'APP 节点与 Docker 负载' in response.text


def test_provider_controls_and_node_assignment():
    from types import SimpleNamespace
    from jinja2 import Environment, FileSystemLoader
    from pathlib import Path
    import re
    env = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / 'src/codex_gateway/templates'), autoescape=True)
    from codex_gateway.i18n import t
    env.globals.update(t=t, lang='CN', html_lang='zh-CN')
    workers = [SimpleNamespace(id=provider, name=provider, provider=provider, node_id='app-2', owner_username='owner', status=SimpleNamespace(value='ready'), enabled=True, account_email='account@example.test', auth_mode='oauth', plan_type=None, rate_limits=None, failure_reason=None, retry_after=None, account_checked_at=None) for provider in ('gemini','claude')]
    html = env.get_template('shared/contributions.html').render(workers=workers, csrf=lambda:'', identity=SimpleNamespace(username='owner'), credited=set(), duplicates=set())
    assert 'app-2' in html
    assert '/user/workers/gemini/provider-login/logout' not in html
    assert '/user/workers/claude/provider-login/logout' not in html
    assert re.search(r'action="/user/workers/gemini/login"[^>]*>.*?name="force" value="true"', html)
