from page_helpers import rendered_pages
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from codex_gateway import main
from codex_gateway.database import SessionLocal
from codex_gateway.models import Worker, ResponseBinding, ApiKey
from codex_gateway.quota import quota_summary
from test_quota_workers import worker_services, create_person
from test_self_service import user_login, AJAX


def test_claude_login_quota_windows_and_logout(worker_services, monkeypatch):
    from codex_gateway import gemini_backend, claude_backend
    account = {"type": "claude-subscription", "email": "claude@example.test", "planType": "team", "project": "org"}
    calls = []
    async def rpc(endpoint, settings, path, payload=None):
        calls.append((path, payload))
        if path == "/login/start":
            return {"session_id": "login-a", "stage": "authorize", "login_url": "https://claude.com/cai/oauth/authorize?test=1"}
        if path == "/login/status":
            return {"session_id": "login-a", "stage": "done", "logged_in": True, "account": account}
        if path == "/login/verify":
            return {"account": account, "available": True}
        if path == "/rate-limits":
            return {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 20},
                                   "secondary": {"windowDurationMins": 10080, "usedPercent": 40}}}
        if path == "/login/logout":
            return {"logged_in": False, "account": None}
        raise AssertionError(path)
    monkeypatch.setattr(gemini_backend, "worker_rpc", rpc)
    monkeypatch.setattr(claude_backend, "worker_rpc", rpc)
    async def inspect(owner, worker_id):
        async with SessionLocal() as db:
            worker = await db.get(Worker, UUID(worker_id))
            return await quota_summary(db, owner), worker.execution_generation
    with TestClient(main.app) as client:
        owner, pw = create_person(client)
        csrf = user_login(client, owner, pw)
        response = client.post("/user/workers", data={"provider": "claude", "csrf_token": csrf}, headers=AJAX)
        assert response.status_code == 200, response.text
        worker_id = response.json()["worker_id"]
        page = rendered_pages(client, "/user/workers").text
        assert 'data-provider="claude"' in page and "Claude / Claude Code" in page
        base = "/user/workers/" + worker_id
        response = client.post(base + "/provider-login/start", data={"csrf_token": csrf}, headers=AJAX)
        assert response.json()["stage"] == "authorize"
        response = client.post(base + "/provider-login/status", data={"csrf_token": csrf, "session_id": "login-a"}, headers=AJAX)
        assert response.json()["verification"]["ok"]
        q, generation = client.portal.call(inspect, owner, worker_id)
        assert q["contributed"] == 1
        assert "Claude · team" in rendered_pages(client, "/user/workers").text
        response = client.post(base + "/rate-limits", data={"csrf_token": csrf}, headers=AJAX)
        assert response.json()["buckets"][0]["five_hour"]["used"] == 20
        assert response.json()["buckets"][0]["week"]["used"] == 40
        assert client.post(base + "/provider-login/logout", data={"csrf_token": "wrong"}, headers=AJAX).status_code == 403
        assert client.post(base + "/provider-login/logout", data={"csrf_token": csrf}, headers=AJAX).status_code == 200
        q, after = client.portal.call(inspect, owner, worker_id)
        assert q["contributed"] == 0 and after > generation


def test_claude_limit_preserves_credit_and_account_change_invalidates_binding():
    from codex_gateway.contributions import update_account
    from codex_gateway.quota import reconcile_worker
    from codex_gateway.models import User, WorkerStatus
    async def check():
        async with SessionLocal() as db:
            owner = "claude-quota-" + uuid4().hex[:10]
            db.add(User(username=owner, enabled=True))
            await db.flush()
            worker = Worker(name=owner, container_name=owner, owner_username=owner, provider="claude",
                endpoint="http://worker", status=WorkerStatus.ready, enabled=True,
                auth_mode="claude-subscription", plan_type="max", account_email="first@example.test",
                account_checked_at=datetime.now(timezone.utc))
            key = ApiKey(name=owner, prefix=owner, key_hash="test", owner_username=owner, enabled=True)
            db.add_all([worker, key])
            await db.flush()
            binding = ResponseBinding(response_id="resp_" + uuid4().hex, api_key_id=key.id,
                worker_id=worker.id, thread_id=str(uuid4()), provider="claude", status="active")
            db.add(binding)
            await db.flush()
            worker.status, worker.failure_kind = WorkerStatus.error, "limit"
            await reconcile_worker(db, worker)
            assert (await quota_summary(db, owner))["contributed"] == 1
            assert key.enabled
            await update_account(worker, {"type": "claude-subscription", "email": "second@example.test", "planType": "max"})
            await db.refresh(binding)
            assert binding.status == "invalid"
            worker.auth_mode = None
            await reconcile_worker(db, worker)
            assert (await quota_summary(db, owner))["contributed"] == 0
            assert not key.enabled
            await db.rollback()
    with TestClient(main.app) as client:
        client.portal.call(check)
