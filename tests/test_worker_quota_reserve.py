from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from codex_gateway import main
from codex_gateway.auth import ApiPrincipal
from codex_gateway.rate_limits import quota_reserve_reached


@pytest.mark.parametrize("used,blocked", [(79.99, False), (80, True), (81, True), (100, True), (None, False)])
@pytest.mark.parametrize("window", ["five_hour_used", "week_used"])
def test_reserve_boundary(window, used, blocked):
    assert quota_reserve_reached({window: used}, 20) is blocked


def test_custom_reserve():
    assert quota_reserve_reached({"week_used": 70}, 30)
    assert not quota_reserve_reached({"week_used": 69.99}, 30)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["new", "recent", "bound", "pinned"])
@pytest.mark.parametrize("provider", ["codex", "claude", "gemini"])
@pytest.mark.parametrize("window", ["five_hour_used", "week_used"])
async def test_reserve_applies_to_all_routes(monkeypatch, route, provider, window):
    workers = [SimpleNamespace(id=uuid4(), provider=provider, enabled=True,
                               status="ready", container_name=name, endpoint=f"http://{name}",
                               execution_generation=0) for name in ("reserved", "available")]
    class Session:
        async def scalars(self, statement):
            return SimpleNamespace(all=lambda: workers)
        async def scalar(self, statement):
            return workers[0].id if route == "recent" else None
        async def get(self, model, key):
            return next(w for w in workers if w.id == key)

    async def signals(*args):
        return {}, {"workers": {str(workers[0].id): {window: 80},
                                str(workers[1].id): {window: 79.99}}}, set()

    attempted = []
    async def put(client, url, **kwargs):
        attempted.append(url)
        return httpx.Response(200)

    monkeypatch.setattr(main, "pool_routing_signals", signals)
    monkeypatch.setattr(main.httpx.AsyncClient, "put", put)
    principal = ApiPrincipal(uuid4(), "test", pinned_worker_id=workers[0].id if route == "pinned" else None)
    binding = SimpleNamespace(worker_id=workers[0].id, provider=provider) if route == "bound" else None
    if route in {"bound", "pinned"}:
        with pytest.raises(HTTPException) as exc:
            await main.choose_target(principal, Session(), binding=binding, provider=provider)
        assert exc.value.status_code == 503
        assert exc.value.detail["error"]["code"] == ("session_worker_unavailable" if binding else "worker_unavailable")
        assert not attempted
    else:
        target = await main.choose_target(principal, Session(), provider=provider)
        assert target.worker_id == workers[1].id
        assert len(attempted) == 1
        assert "/workers/available/" in attempted[0]

