"""Isolation verdicts and recovery must preserve deliberate user decisions."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from codex_gateway import main
from codex_gateway.config import get_settings
from codex_gateway.database import SessionLocal
from codex_gateway.models import ApiKey, User, Worker, WorkerStatus
from codex_gateway.quota import mark_key_disabled, reconcile_worker, quota_summary
from test_quota_workers import (
    worker_services, create_person, user_login, contribute, probe, new_key,
    summary, eligible_worker, AJAX,
)


def test_refresh_failure_immediately_isolates_and_pauses_key(worker_services, monkeypatch):
    import codex_gateway.contributions as contributions
    calls = []
    class Server:
        async def call(self, method, params):
            calls.append(params)
            raise TimeoutError("refresh unavailable")
    @asynccontextmanager
    async def opened(*args, **kwargs):
        yield Server()
    with TestClient(main.app) as client:
        owner, password = create_person(client)
        token = user_login(client, owner, password)
        wid = contribute(client, token)
        probe(client, token, wid)
        key = new_key(client, token).json()
        monkeypatch.setattr(contributions, "open_app_server", opened)
        client.portal.call(contributions.refresh_worker_account, UUID(wid))
        assert calls == [{"refreshToken": True}]
        assert client.portal.call(summary, owner)["used"] == 0
        async def check():
            async with SessionLocal() as db:
                assert (await db.get(Worker, UUID(wid))).status == WorkerStatus.error
                k = await db.get(ApiKey, UUID(key["key_id"]))
                assert not k.enabled and k.disabled_reason == "auto_quota"
        client.portal.call(check)


def test_confirmed_failure_restores_only_automatic_key(worker_services):
    with TestClient(main.app) as client:
        owner, password = create_person(client)
        token = user_login(client, owner, password)
        wid = contribute(client, token)
        probe(client, token, wid)
        manual = new_key(client, token).json()["key_id"]
        assert client.post("/user/account/keys/"+manual+"/toggle",
                           data={"csrf_token": token}, headers=AJAX).status_code == 200
        automatic = new_key(client, token).json()["key_id"]
        worker_services["failure"] = True
        assert not probe(client, token, wid)["ok"]
        async def check(stopped):
            async with SessionLocal() as db:
                k = await db.get(ApiKey, UUID(automatic))
                assert k.enabled is not stopped
                assert k.disabled_reason == "auto_quota" and k.disabled_at
                m = await db.get(ApiKey, UUID(manual))
                assert not m.enabled and m.disabled_reason == "manual_user"
        client.portal.call(check, True)
        worker_services["failure"] = False
        assert probe(client, token, wid)["ok"]
        client.portal.call(check, False)
        # A subsequent manual stop replaces the automatic marker.
        assert client.post("/user/account/keys/"+automatic+"/toggle",
                           data={"csrf_token": token}, headers=AJAX).status_code == 200
        assert probe(client, token, wid)["ok"]
        assert client.portal.call(summary, owner)["used"] == 0


def test_recovery_capacity_exclusions_and_idempotence():
    async def check():
        async with SessionLocal() as db:
            owner = "recovery-" + uuid4().hex[:10]
            db.add(User(username=owner, enabled=True))
            await db.flush()
            w = eligible_worker(owner, owner+"@example.com")
            db.add(w)
            keys = []
            for reason in ["auto_quota", "auto_quota", "manual_admin", None, "auto_quota", "auto_quota"]:
                k = ApiKey(name=owner, prefix=uuid4().hex[:16], key_hash=uuid4().hex,
                           owner_username=owner, enabled=False, disabled_reason=reason)
                db.add(k)
                keys.append(k)
            keys[0].last_used_at = datetime.now(timezone.utc)
            keys[4].admin_disabled = True
            keys[5].deleted_at = datetime.now(timezone.utc)
            await reconcile_worker(db, w)
            assert [k.enabled for k in keys] == [True, False, False, False, False, False]
            await reconcile_worker(db, w)
            assert (await quota_summary(db, owner))["used"] == 1
            mark_key_disabled(keys[0], "manual_user")
            user = await db.get(User, owner)
            user.enabled = False
            await reconcile_worker(db, w)
            assert not any(k.enabled for k in keys)
            await db.rollback()
    with TestClient(main.app) as client:
        client.portal.call(check)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini"])
async def test_busy_recovery_preserves_isolation(monkeypatch, provider):
    import importlib
    module = importlib.import_module("codex_gateway."+provider+"_backend")
    worker = SimpleNamespace(id=uuid4(), endpoint="http://worker", provider=provider,
                             status=WorkerStatus.error, auth_mode="subscription", enabled=True,
                             failure_kind="connection", failure_reason="original failure")
    response = httpx.Response(409, request=httpx.Request("POST", worker.endpoint))
    rpc = AsyncMock(side_effect=httpx.HTTPStatusError("busy", request=response.request, response=response))
    monkeypatch.setattr(module, "worker_rpc", rpc)
    reconcile = AsyncMock()
    monkeypatch.setattr("codex_gateway.quota.reconcile_worker", reconcile)
    result = await getattr(module, "probe_"+provider)(
        worker, SimpleNamespace(commit=AsyncMock()), get_settings())
    assert result["busy"] and not result["ok"]
    assert worker.status == WorkerStatus.error
    assert worker.failure_reason == "original failure"
    assert not main.worker_accepts_request(worker)
    reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_continues_after_one_worker_failure(monkeypatch):
    ids = [uuid4(), uuid4()]
    db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: ids)), commit=AsyncMock())
    @asynccontextmanager
    async def session():
        yield db
    monkeypatch.setattr(main, "SessionLocal", session)
    monkeypatch.setattr(main, "expire_response_bindings", AsyncMock())
    refresh = AsyncMock(side_effect=[RuntimeError("one worker failed"), None])
    monkeypatch.setattr("codex_gateway.contributions.refresh_worker_account", refresh)
    await main.worker_recovery_once()
    assert [call.args[0] for call in refresh.await_args_list] == ids


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["interrupted", None, "no_event"])
async def test_incomplete_healthcheck_is_not_recovery(status):
    from codex_gateway.backend import run_healthcheck_turn, WorkerFailure
    class Server:
        async def call(self, method, params):
            return {"thread": {"id": "health"}} if method == "thread/start" else {}
        async def messages(self):
            if status != "no_event":
                yield {"method": "turn/completed", "params": {"turn": {"status": status}}}
    with pytest.raises(WorkerFailure):
        await run_healthcheck_turn(Server(), "test-model")



def test_automatic_recovery_uses_manual_probe_and_respects_cooldown(worker_services, monkeypatch):
    import codex_gateway.admin as admin
    import codex_gateway.contributions as contributions
    params_seen = []
    class Server:
        async def call(self, method, params):
            params_seen.append(params)
            assert params == {"refreshToken": False}
            return {"account": worker_services["account"]}
    @asynccontextmanager
    async def opened(*args, **kwargs):
        yield Server()
    with TestClient(main.app) as client:
        owner, password = create_person(client)
        token = user_login(client, owner, password)
        wid = contribute(client, token)
        probe(client, token, wid)
        new_key(client, token)
        worker_services["failure"] = True
        probe(client, token, wid)
        monkeypatch.setattr(admin, "open_app_server", opened)
        client.portal.call(contributions.refresh_worker_account, UUID(wid))
        assert not params_seen  # Failed probes must honor their cooldown.
        async def expire_cooldown():
            async with SessionLocal() as db:
                worker = await db.get(Worker, UUID(wid))
                worker.retry_after = datetime.now(timezone.utc) - timedelta(seconds=1)
                await db.commit()
        client.portal.call(expire_cooldown)
        client.portal.call(contributions.refresh_worker_account, UUID(wid))
        assert params_seen == [{"refreshToken": False}]
        assert client.portal.call(summary, owner)["used"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "claude", "gemini"])
@pytest.mark.parametrize("kind", ["limit", "logged_out", "ineligible", "connection"])
async def test_every_reported_worker_failure_isolates_without_probe(monkeypatch, provider, kind):
    worker = SimpleNamespace(provider=provider, failure_kind=None, failure_reason=None)
    db = SimpleNamespace(scalar=AsyncMock(return_value=worker), commit=AsyncMock())
    @asynccontextmanager
    async def session():
        yield db
    monkeypatch.setattr(main, "SessionLocal", session)
    monkeypatch.setattr("codex_gateway.quota.quota_lock", AsyncMock())
    probe = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(main, "probe_worker_record", probe)
    reconcile = AsyncMock()
    monkeypatch.setattr(main, "reconcile_worker", reconcile)
    assert await main.quarantine_worker(uuid4(), "reported failure", kind)
    assert worker.status == WorkerStatus.error and worker.failure_kind == kind
    assert worker.retry_after > worker.quarantined_at
    probe.assert_not_awaited()
    reconcile.assert_awaited_once_with(db, worker)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_codex_probe_total_deadline(monkeypatch):
    import asyncio
    import codex_gateway.admin as admin
    worker = SimpleNamespace(id=uuid4(), provider="codex", endpoint="ws://worker",
                             auth_mode="chatgpt", plan_type="plus", status=WorkerStatus.ready)
    class Server:
        async def call(self, method, params):
            return {"account": {"type": "chatgpt", "planType": "plus"}}
    @asynccontextmanager
    async def opened(*args, **kwargs):
        yield Server()
    async def hanging_turn(*args):
        await asyncio.sleep(10)
    settings = get_settings().model_copy(update={"worker_probe_timeout_seconds": 0.01})
    monkeypatch.setattr(admin, "open_app_server", opened)
    monkeypatch.setattr(admin, "run_healthcheck_turn", hanging_turn)
    monkeypatch.setattr(admin, "quota_lock", AsyncMock())
    monkeypatch.setattr(admin, "reconcile_worker", AsyncMock())
    monkeypatch.setattr("codex_gateway.contributions.update_account", AsyncMock())
    db = SimpleNamespace(commit=AsyncMock())
    result = await admin.probe_worker_record(worker, db, settings)
    assert not result["ok"] and worker.status == WorkerStatus.error
    assert worker.failure_reason == "TimeoutError"
    assert worker.retry_after > worker.quarantined_at


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "gemini"])
async def test_unavailable_provider_cannot_recover_from_account_only(monkeypatch, provider):
    import importlib
    module = importlib.import_module("codex_gateway."+provider+"_backend")
    worker = SimpleNamespace(id=uuid4(), endpoint="http://worker", provider=provider,
                             status=WorkerStatus.error, auth_mode="subscription",
                             failure_kind="connection", failure_reason="old failure")
    rpc = AsyncMock(return_value={"account": {"email": "test@example.com"}, "available": True})
    monkeypatch.setattr(module, "worker_rpc", rpc)
    monkeypatch.setattr("codex_gateway.contributions.update_account", AsyncMock())
    monkeypatch.setattr("codex_gateway.quota.reconcile_worker", AsyncMock())
    result = await getattr(module, "probe_"+provider)(
        worker, SimpleNamespace(commit=AsyncMock()), get_settings(), inference=False)
    assert result["ok"] and worker.recovered_at
    assert rpc.await_args.args[2] == "/probe"
