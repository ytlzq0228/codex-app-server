import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from codex_gateway import dchat, notifications
from codex_gateway.database import SessionLocal
from codex_gateway.main import app
from codex_gateway.models import DChatConfig, Worker, WorkerNotification, WorkerStatus
from codex_gateway.quota import reconcile_worker
from page_helpers import rendered_pages
from test_self_service import admin_login, new_user, user_login, AJAX


@pytest.mark.asyncio
async def test_transport_and_profile_cache(monkeypatch):
    config = DChatConfig(enabled=True, bot_id="test-bot", api_client_id="test-client",
                         api_client_secret="fixture-secret")
    calls = []
    response_body = {"success": True}
    status = 200

    def handle(request):
        calls.append(request)
        assert request.headers["authorization"].startswith("Basic ")
        if request.method == "GET":
            assert request.url.params["bot_id"] == "test-bot"
            return httpx.Response(200, json={"result": {"full_name": "Alice <Admin>"}})
        assert json.loads(request.content)["username"] == "alice"
        return httpx.Response(status, json=response_body)

    original = httpx.AsyncClient
    monkeypatch.setattr(dchat.httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    assert (await dchat.send_text_message(config, "alice", "hello"))["success"]
    response_body = {"success": False}
    assert not (await dchat.send_text_message(config, "alice", "hello"))["success"]
    status = 503
    assert (await dchat.send_text_message(config, "alice", "hello"))["error"] == "HTTP 503"
    assert not (await dchat.send_text_message(None, "alice", "hello"))["success"]

    class DB:
        async def get(self, *args):
            return config

    dchat._profiles.clear()
    assert await dchat.display_names(DB(), ["alice", "alice"]) == {"alice": "Alice <Admin>"}
    count = len(calls)
    assert await dchat.display_names(DB(), ["alice"]) == {"alice": "Alice <Admin>"}
    assert len(calls) == count
    config.api_client_secret = "rotated-fixture"
    await dchat.display_names(DB(), ["alice"])
    assert len(calls) == count + 1
    config.enabled = False
    assert await dchat.display_names(DB(), ["alice"]) == {"alice": "alice"}


@pytest.mark.asyncio
async def test_lookup_failure_falls_back(monkeypatch):
    config = DChatConfig(enabled=True, bot_id="bot", api_client_id="id", api_client_secret="secret")
    original = httpx.AsyncClient
    for body in ([], {"result": []}, {"error": "denied"}, {"result": {"full_name": 42}}):
        monkeypatch.setattr(dchat.httpx, "AsyncClient", lambda **kw: original(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)), **kw))
        dchat._profiles.clear()
        class DB:
            async def get(self, *args):
                return config
        assert await dchat.display_names(DB(), ["alice"]) == {"alice": "alice"}


def test_admin_settings_and_display_names(monkeypatch):
    async def no_send(*args, **kwargs):
        return {"success": True}
    monkeypatch.setattr(dchat, "send_text_message", no_send)
    async def lookup(config, username):
        return {"full_name": "Display <" + username + ">"}
    monkeypatch.setattr(dchat, "get_user_info", lookup)
    dchat._profiles.clear()
    with TestClient(app) as client:
        token = admin_login(client)
        assert client.post("/admin/dchat", data={"csrf_token": "bad"}, headers=AJAX).status_code == 403
        assert client.post("/admin/dchat", data={"csrf_token": token, "enabled": "true"}, headers=AJAX).status_code == 400
        form = dict(csrf_token=token, enabled="true", bot_id="test-bot", api_client_id="test-client",
                    api_client_secret="fixture-secret")
        assert client.post("/admin/dchat", data=form, headers=AJAX).status_code == 200
        form["api_client_secret"] = ""
        assert client.post("/admin/dchat", data=form, headers=AJAX).status_code == 200
        page = client.get("/admin/dchat/data")
        assert page.json()["config"]["has_secret"]
        assert "fixture-secret" not in page.text
        html = rendered_pages(client, "/admin/dchat").text
        assert 'name="bot_id"' in html and 'name="base_url"' not in html
        assert "fixture-secret" not in html
        username, password = new_user(client, token)
        users = rendered_pages(client, "/admin/users").text
        assert "Display &lt;" + username + "&gt;" in users
        assert "/admin/users/" + username in users
        options = client.get("/admin/options/users", params={"search": username}).json()["options"]
        assert options[0]["value"] == username and options[0]["label"] == "Display <" + username + ">"
        workers = rendered_pages(client, "/admin/workers").text
        assert "Display &lt;admin&gt;" in workers
        user_login(client, username, password)
        assert client.get("/admin/dchat/data").status_code == 403
        assert client.post("/admin/dchat", data=form, headers=AJAX).status_code == 403

        async def disable():
            async with SessionLocal() as db:
                config = await db.get(DChatConfig, 1)
                config.enabled = False
                await db.commit()
        client.portal.call(disable)


def test_durable_notification_retry_recovery_and_rollback(monkeypatch):
    sent = []
    succeeds = False
    async def send(db, username, text):
        sent.append((username, text))
        return {"success": succeeds}
    monkeypatch.setattr(notifications, "send_message", send)

    async def scenario():
        worker_id = uuid4()
        async with SessionLocal() as db:
            worker = Worker(id=worker_id, owner_username="admin", name="notice-" + worker_id.hex,
                            container_name="notice-" + worker_id.hex, endpoint="ws://example.com",
                            status=WorkerStatus.error, failure_kind="logged_out", enabled=True)
            db.add(worker)
            await reconcile_worker(db, worker)
            await db.commit()
        async with SessionLocal() as db:
            assert not await notifications.deliver_worker_notification(db, worker_id)
            notice = await db.get(WorkerNotification, worker_id)
            assert notice.sent_at is None and notice.retry_at
        async with SessionLocal() as db:
            assert not await notifications.deliver_worker_notification(db, worker_id)
        assert len(sent) == 1
        nonlocal succeeds
        succeeds = True
        async with SessionLocal() as db:
            notice = await db.get(WorkerNotification, worker_id)
            notice.retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
        async with SessionLocal() as db:
            assert await notifications.deliver_worker_notification(db, worker_id)
        async with SessionLocal() as db:
            worker = await db.get(Worker, worker_id)
            await reconcile_worker(db, worker)
            await db.commit()
            assert not await notifications.deliver_worker_notification(db, worker_id)
        assert len(sent) == 2
        assert sent[0][0] == "admin" and "/user/workers" in sent[0][1]
        async with SessionLocal() as db:
            worker = await db.get(Worker, worker_id)
            worker.status = WorkerStatus.ready
            await reconcile_worker(db, worker)
            await db.commit()
            worker.status = WorkerStatus.error
            await reconcile_worker(db, worker)
            await db.rollback()
        async with SessionLocal() as db:
            assert not await notifications.deliver_worker_notification(db, worker_id)
            worker = await db.get(Worker, worker_id)
            worker.status = WorkerStatus.error
            await reconcile_worker(db, worker)
            await db.commit()
        async with SessionLocal() as db:
            assert await notifications.deliver_worker_notification(db, worker_id)
        assert len(sent) == 3
        async with SessionLocal() as db:
            worker = await db.get(Worker, worker_id)
            worker.enabled = False
            await reconcile_worker(db, worker)
            await db.commit()
            assert not await notifications.deliver_worker_notification(db, worker_id)

    with TestClient(app) as client:
        client.portal.call(scenario)


def test_concurrent_delivery_and_removed_worker(monkeypatch):
    started = None
    release = None
    calls = []
    async def send(db, username, text):
        calls.append(username)
        started.set()
        await release.wait()
        return {"success": True}
    monkeypatch.setattr(notifications, "send_message", send)

    async def scenario():
        nonlocal started, release
        started, release = asyncio.Event(), asyncio.Event()
        worker_id = uuid4()
        async with SessionLocal() as db:
            worker = Worker(id=worker_id, name="parallel-" + worker_id.hex,
                container_name="parallel-" + worker_id.hex, owner_username="admin",
                endpoint="ws://example.com", status=WorkerStatus.error, enabled=True)
            db.add(worker)
            await reconcile_worker(db, worker)
            await db.commit()
        async def deliver():
            async with SessionLocal() as db:
                return await notifications.deliver_worker_notification(db, worker_id)
        first = asyncio.create_task(deliver())
        await asyncio.wait_for(started.wait(), 2)
        try:
            assert not await asyncio.wait_for(deliver(), 2)
        finally:
            release.set()
        assert await first
        assert calls == ["admin"]
        async with SessionLocal() as db:
            worker = await db.get(Worker, worker_id)
            worker.status = WorkerStatus.ready
            await reconcile_worker(db, worker)
            await db.commit()
            worker.status = WorkerStatus.error
            await reconcile_worker(db, worker)
            await db.commit()
            worker.endpoint = "removed://worker"
            await db.commit()
        assert not await deliver()
        assert calls == ["admin"]
    with TestClient(app) as client:
        client.portal.call(scenario)
