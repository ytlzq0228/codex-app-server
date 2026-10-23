import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx

import docker
import pytest
from fastapi import HTTPException

from codex_gateway.contributions import update_account
from codex_gateway.models import Worker
from codex_gateway.retention_policy import RETENTION, stopped_since
from codex_gateway.worker_retention import expired_logout


@pytest.mark.asyncio
async def test_continuous_logout_and_relogin():
    worker = Worker(name="example", container_name="example", endpoint="ws://example:4500")
    await update_account(worker, None)
    first = worker.logged_out_since
    await update_account(worker, None)
    assert worker.logged_out_since == first
    assert not expired_logout(worker, first + RETENTION - timedelta(seconds=1))
    assert expired_logout(worker, first + RETENTION)
    await update_account(worker, {"type": "chatgpt", "email": "test@example.com"})
    assert worker.logged_out_since is None
    assert not expired_logout(worker, first + RETENTION)
    await update_account(worker, None)
    assert worker.logged_out_since >= first


@pytest.mark.parametrize("status,finished,valid", [
    ("exited", "2026-10-01T10:00:00.123456789Z", True),
    ("dead", "2026-10-01T10:00:00Z", True),
    ("running", "2026-10-01T10:00:00Z", False),
    ("paused", "2026-10-01T10:00:00Z", False),
    ("created", "0001-01-01T00:00:00Z", False),
    ("exited", "bad", False),
    ("exited", "0001-01-01T00:00:00Z", False),
])
def test_stop_time(status, finished, valid):
    value = stopped_since({"State": {"Status": status, "FinishedAt": finished}})
    assert (value is not None) == valid


@pytest.fixture
def manager(monkeypatch):
    container = Mock(id="old-id", labels={"io.codex-gateway.managed": "true"})
    container.attrs = {"State": {"Status": "exited", "FinishedAt":
        (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()}}
    client = SimpleNamespace(containers=SimpleNamespace(get=lambda name: container))
    monkeypatch.setattr(docker, "from_env", lambda: client)
    module = importlib.import_module("codex_gateway.manager")
    monkeypatch.setattr(module, "client", client)
    monkeypatch.setenv("CODEX_MANAGER_TOKEN", "retention-test-token-0123456789")
    return module, container


def remove_stopped(manager, **kwargs):
    manager.delete_worker("example", "Bearer retention-test-token-0123456789",
        expected_id="old-id", stopped_before=datetime.now(timezone.utc) - RETENTION, **kwargs)


def test_stop_cleanup_preserves_volumes(manager):
    module, container = manager
    remove_stopped(module)
    container.remove.assert_called_once_with(force=False)


@pytest.mark.parametrize("change", ["restart", "replace", "recent", "unmanaged"])
def test_stop_cleanup_rechecks_state_and_identity(manager, change):
    module, container = manager
    if change == "restart":
        container.attrs["State"]["Status"] = "running"
    elif change == "replace":
        container.id = "new-id"
    elif change == "recent":
        container.attrs["State"]["FinishedAt"] = datetime.now(timezone.utc).isoformat()
    else:
        container.labels = {}
    with pytest.raises(HTTPException):
        remove_stopped(module)
    container.remove.assert_not_called()


def test_stop_cleanup_failure(manager):
    module, container = manager
    container.remove.side_effect = docker.errors.APIError("running again")
    with pytest.raises(HTTPException) as exc:
        remove_stopped(module)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,deleted,archived", [
    ("stopped", True, True), ("backup", True, False),
    ("recent", False, False), ("delete_failed", True, False),
    ("logged_out", True, True), ("relogin", False, False),
    ("connection_failed", False, False), ("unknown", False, False),
    ("busy", False, False), ("missing", False, False),
])
async def test_retention_sweep(monkeypatch, scenario, deleted, archived):
    from codex_gateway import admin, worker_retention as retention
    now = datetime.now(timezone.utc)
    is_running = scenario in {"logged_out", "relogin", "connection_failed", "unknown", "busy", "missing"}
    worker = Worker(id=uuid4(), name="example", container_name="example",
                    endpoint="ws://example:4500", auth_mode=None if is_running else "chatgpt",
                    logged_out_since=None if scenario == "unknown" else now - timedelta(days=8))
    row = {"name": "example", "id": "container-id", "state": "running" if is_running else "exited",
           "stopped_since": None if is_running else (now - timedelta(days=1 if scenario == "recent" else 8)).isoformat()}
    db = AsyncMock()
    db.__aenter__.return_value = db
    db.scalars.return_value = SimpleNamespace(all=lambda: [] if scenario == "backup" else [worker.id])
    db.scalar.return_value = None if scenario == "backup" else worker
    monkeypatch.setattr(retention, "SessionLocal", lambda: db)
    monkeypatch.setattr(retention, "quota_lock", AsyncMock())
    archive = AsyncMock()
    monkeypatch.setattr(retention, "archive_worker", archive)

    async def probe(worker, db, settings):
        if scenario == "busy":
            return {"busy": True}
        if scenario == "relogin":
            await update_account(worker, {"type": "chatgpt"})
        elif scenario == "connection_failed":
            worker.failure_kind = "connection"
        else:
            await update_account(worker, None)
            worker.failure_kind = "logged_out"
    monkeypatch.setattr(admin, "probe_worker_record", probe)
    deletes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=[] if scenario == "missing" else [row])
        deletes.append(request)
        return httpx.Response(409 if scenario == "delete_failed" else 204)

    original = httpx.AsyncClient
    monkeypatch.setattr(retention.httpx, "AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    settings = SimpleNamespace(node_id="", manager_url="http://manager",
                               manager_token=SimpleNamespace(get_secret_value=lambda: "test"))
    await retention.cleanup_once(settings)
    assert bool(deletes) is deleted
    assert bool(archive.await_count) is archived
    if deletes:
        assert deletes[0].url.params["expected_id"] == "container-id"
        assert ("stopped_before" in deletes[0].url.params) is (not is_running)
