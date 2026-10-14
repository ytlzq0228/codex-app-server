from types import SimpleNamespace

import httpx
import pytest

from codex_gateway.app_server import AppServerPool, AppServerSlot
from codex_gateway.claude_backend import ClaudeAdapter
from codex_gateway.config import Settings
from codex_gateway import cluster
from codex_gateway.backend import BackendTarget, WorkerFailure
from codex_gateway.schemas import ResponseRequest, BackendStreamEvent, BackendResult


def test_claude_load_counts_key_and_worker_capacity():
    adapter = ClaudeAdapter(Settings())
    adapter.active_turns.update({"a:one": 10, "b:one": 1, "a:two": 9,
                                 "b:two": 10, "c:two": 10, "d:two": 10})
    assert adapter.routing_load("a") == {"active": {"one": 11, "two": 39}, "saturated": ["one"]}
    assert adapter.routing_load("new")["saturated"] == []
    adapter.active_turns["e:two"] += 1
    assert adapter.routing_load("new")["saturated"] == ["two"]
    assert adapter.routing_load("b")["saturated"] == ["two"]


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation,pool_fallback", [(False, True), (False, False), (True, True)])
async def test_capacity_race_retries_only_new_pooled_turns(monkeypatch, continuation, pool_fallback):
    adapter = ClaudeAdapter(Settings())
    adapter.active_turns["a:one"] = 10
    called = []
    async def events(*args):
        called.append(True)
        yield BackendStreamEvent(done=True)
    monkeypatch.setattr(adapter, "_worker_events", events)
    target = BackendTarget("a:one", "http://worker", "/workspace/a", provider="claude",
                           allow_capacity_fallback=pool_fallback)
    request = ResponseRequest(model="claude-test", input="hi", previous_response_id="thread" if continuation else None)
    if pool_fallback and not continuation:
        with pytest.raises(WorkerFailure) as failure:
            async for _ in adapter._turn_events(request, target):
                pass
        assert failure.value.safe_to_retry
        assert failure.value.kind == "capacity"
        assert not called
    else:
        assert [event.done async for event in adapter._turn_events(request, target)] == [True]
        assert called
    assert adapter.active_turns["a:one"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_retry", [True, False])
async def test_nonstream_capacity_failover_preserves_pinned_requests(monkeypatch, allow_retry):
    import codex_gateway.main as main
    from codex_gateway.auth import ApiPrincipal
    from fastapi import HTTPException
    first = BackendTarget("a:one", "http://one", "/workspace/a", provider="claude")
    second = BackendTarget("a:two", "http://two", "/workspace/a", provider="claude")
    calls = []
    class Backend:
        async def complete(self, request, target):
            calls.append(target)
            if target == first:
                raise WorkerFailure("full", kind="capacity", code="worker_capacity_exceeded", status=503, safe_to_retry=True)
            return BackendResult(text="ok", thread_id="thread")
    async def retry(*args):
        return second
    monkeypatch.setattr(main, "retry_target", retry)
    args = (ResponseRequest(model="claude-test", input="hi"), Backend(), ApiPrincipal(None, "test"), first)
    if allow_retry:
        result, target = await main.complete_with_failover(*args, allow_retry=True)
        assert result.text == "ok" and target == second and calls == [first, second]
    else:
        with pytest.raises(HTTPException) as failure:
            await main.complete_with_failover(*args, allow_retry=False)
        assert failure.value.status_code == 503 and calls == [first]


@pytest.mark.asyncio
async def test_codex_routing_uses_actual_pool_availability():
    pool = AppServerPool("token", 1, max_per_group=2, max_per_worker=3)
    import time
    from unittest.mock import AsyncMock
    pool._slots = [AppServerSlot(group_key=key, worker_key="worker", slot_id=i,
                                session=SimpleNamespace(closed=False, close=AsyncMock()),
                                busy=busy, last_used=time.monotonic())
                   for i, (key, busy) in enumerate([("a:worker", True), ("a:worker", True), ("b:worker", False)])]
    assert await pool.routing_load("a") == {"active": {"worker": 2}, "saturated": ["worker"]}
    assert (await pool.routing_load("b"))["saturated"] == []
    assert (await pool.routing_load("c"))["saturated"] == ["worker"]
    await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_down", [False, True])
async def test_cluster_load_uses_owner_key_counts_and_falls_back_to_heartbeat(monkeypatch, remote_down):
    settings = Settings(node_id="local", node_gateway_url="http://local", node_manager_url="http://manager",
                        bootstrap_worker=False)
    nodes = [SimpleNamespace(id="local", active_connections={"one": 99}),
             SimpleNamespace(id="remote", gateway_url="http://remote", active_connections={"two": 40})]
    async def live(*args):
        return nodes
    class Backend:
        async def routing_load(self, key):
            assert key == "a"
            return {"active": {"one": 2}, "saturated": []}
    async def handler(request):
        assert request.url.path == "/internal/routing-load"
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(503 if remote_down else 200,
                              json={"active": {"two": 10}, "saturated": ["two"]})
    original = httpx.AsyncClient
    monkeypatch.setattr(cluster, "live_nodes", live)
    monkeypatch.setattr(cluster.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    assert await cluster.routing_load(None, Backend(), settings, "a") == {
        "active": {"one": 2, "two": 40 if remote_down else 10}, "saturated": ["two"]}


@pytest.mark.asyncio
async def test_routing_load_endpoint_requires_internal_auth(monkeypatch):
    from fastapi import FastAPI
    from pydantic import SecretStr
    settings = Settings(node_id="local", node_gateway_url="http://local", node_manager_url="http://manager",
                        manager_token=SecretStr("test-only-token"), bootstrap_worker=False)
    monkeypatch.setattr(cluster, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(cluster.router)
    class Backend:
        async def routing_load(self, key):
            assert key == "a"
            return {"active": {"worker": 10}, "saturated": ["worker"]}
    app.state.backend = Backend()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        assert (await client.post("/internal/routing-load", json={"key": "a"})).status_code == 401
        headers = {"Authorization": "Bearer test-only-token"}
        assert (await client.post("/internal/routing-load", headers=headers, json={"key": []})).status_code == 400
        response = await client.post("/internal/routing-load", headers=headers, json={"key": "a"})
        assert response.status_code == 200
        assert response.json() == {"active": {"worker": 10}, "saturated": ["worker"]}
