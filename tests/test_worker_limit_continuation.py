from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from codex_gateway import main
from codex_gateway.auth import ApiPrincipal
from codex_gateway.backend import BackendTarget
from codex_gateway.client_tools import ToolProtocolError


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["limit", "connection", "logged_out", "disabled", "removed", "generation"])
async def test_pending_limit_keeps_call_but_identity_and_health_failures_cancel(case):
    target = BackendTarget("key:worker", "http://worker", "/workspace", uuid4(), 1)
    worker = SimpleNamespace(id=target.worker_id, endpoint=target.endpoint,
                             enabled=case != "disabled", status="error", failure_kind="limit",
                             execution_generation=2 if case == "generation" else 1)
    if case in {"connection", "logged_out"}:
        worker.failure_kind = case
    if case == "removed":
        worker.endpoint = "removed://worker"
    sessions = SimpleNamespace(runs=[SimpleNamespace(target=target, thread_id="thread")],
                               cancel_thread=AsyncMock())
    db = SimpleNamespace(get=AsyncMock(return_value=worker))
    if case == "limit":
        await main.validate_pending_worker(SimpleNamespace(tool_sessions=sessions), target, db)
        sessions.cancel_thread.assert_not_awaited()
        assert sessions.runs[0].target == target
    else:
        with pytest.raises(ToolProtocolError) as exc:
            await main.validate_pending_worker(SimpleNamespace(tool_sessions=sessions), target, db)
        assert exc.value.code == ("tool_worker_invalidated" if case in {"removed", "generation"} else "tool_worker_unavailable")
        sessions.cancel_thread.assert_awaited_once_with("key", "thread")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "claude", "gemini"])
@pytest.mark.parametrize("route", ["new", "bound", "pinned"])
async def test_limited_worker_only_accepts_existing_binding(monkeypatch, provider, route):
    limited = SimpleNamespace(id=uuid4(), provider=provider, enabled=True, status="error",
                              failure_kind="limit", endpoint="http://limited",
                              container_name="limited", execution_generation=1)
    healthy = SimpleNamespace(id=uuid4(), provider=provider, enabled=True, status="ready",
                              endpoint="http://healthy", container_name="healthy", execution_generation=1)
    db = SimpleNamespace(get=AsyncMock(return_value=limited),
                         scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [limited, healthy])))
    monkeypatch.setattr(main, "pool_routing_signals", AsyncMock(return_value=({}, {}, set())))
    put = AsyncMock(return_value=httpx.Response(200))
    monkeypatch.setattr(main.httpx.AsyncClient, "put", put)
    principal = ApiPrincipal(None, "test", pinned_worker_id=limited.id if route == "pinned" else None)
    binding = SimpleNamespace(worker_id=limited.id, provider=provider) if route == "bound" else None
    if route == "pinned":
        with pytest.raises(HTTPException) as exc:
            await main.choose_target(principal, db, binding=binding, provider=provider)
        assert exc.value.status_code == 503
        put.assert_not_awaited()
    else:
        target = await main.choose_target(principal, db, binding=binding, provider=provider)
        assert target.worker_id == (limited.id if binding else healthy.id)
