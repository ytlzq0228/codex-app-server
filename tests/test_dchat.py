import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from fastapi.testclient import TestClient

from codex_gateway import dchat, notifications
from codex_gateway.database import SessionLocal
from codex_gateway.config import get_settings
from codex_gateway.main import app
from codex_gateway.models import DChatConfig, DChatUserCache, User, Worker, WorkerNotification, WorkerStatus
from codex_gateway.quota import reconcile_worker
from page_helpers import rendered_pages
from test_self_service import admin_login, new_user, user_login, AJAX


@pytest_asyncio.fixture
async def profile_cache(monkeypatch):
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(DChatUserCache.__table__.create, checkfirst=True)
        await connection.execute(delete(DChatUserCache))
    monkeypatch.setattr(dchat, "SessionLocal", factory)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_transport_and_profile_cache(monkeypatch, profile_cache):
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
async def test_lookup_failure_falls_back(monkeypatch, profile_cache):
    config = DChatConfig(enabled=True, bot_id="bot", api_client_id="id", api_client_secret="secret")
    original = httpx.AsyncClient
    for body in ([], {"result": []}, {"error": "denied"}, {"result": {"full_name": 42}}):
        monkeypatch.setattr(dchat.httpx, "AsyncClient", lambda **kw: original(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)), **kw))
        class DB:
            async def get(self, *args):
                return config
        assert await dchat.display_names(DB(), ["alice"]) == {"alice": "alice"}


@pytest.mark.asyncio
async def test_persistent_profile_ttl_and_refresh(monkeypatch, profile_cache):
    config = DChatConfig(enabled=True, bot_id="bot", api_client_id="id", api_client_secret="secret")

    class DB:
        async def get(self, *args):
            return config

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(dchat, "datetime", Clock)
    profile = {"full_name": " Alice ", "email": "alice@example.com",
               "department": {"id": 42}, "roles": ["staff"], "optional": None}
    calls = []

    async def lookup(config, name):
        calls.append(name)
        return profile

    monkeypatch.setattr(dchat, "get_user_info", lookup)
    assert await dchat.display_names(DB(), ["alice", "alice"]) == {"alice": "Alice"}
    async with profile_cache() as db:
        entry = (await db.scalars(select(DChatUserCache))).one()
        assert entry.profile == profile
        assert entry.refreshed_at == now
    # A fresh session reads the durable cache; reads do not extend its TTL.
    now += timedelta(days=7) - timedelta(microseconds=1)
    assert await dchat.display_names(DB(), ["alice"]) == {"alice": "Alice"}
    assert calls == ["alice"]
    now += timedelta(microseconds=1)
    profile = {"fullname": "Alice Updated", "new_field": {"enabled": True}}
    assert await dchat.display_names(DB(), ["alice"]) == {"alice": "Alice Updated"}
    assert calls == ["alice", "alice"]
    refreshed_at = now
    async with profile_cache() as db:
        entry = (await db.scalars(select(DChatUserCache))).one()
        assert entry.profile == profile
        assert "email" not in entry.profile
        assert entry.refreshed_at == refreshed_at
    # Failed refreshes must neither replace the full profile nor reset the TTL.
    now += timedelta(days=8)
    saved_profile = profile
    profile = None
    assert await dchat.display_names(DB(), ["alice", "missing"]) == {
        "alice": "alice", "missing": "missing"}
    async with profile_cache() as db:
        entry = (await db.scalars(select(DChatUserCache))).one()
        assert entry.profile == saved_profile
        assert entry.refreshed_at == refreshed_at
    profile = {"full_name": "Recovered"}
    assert await dchat.display_names(DB(), ["alice"]) == {"alice": "Recovered"}


@pytest.mark.asyncio
async def test_profiles_without_display_name_are_cached(monkeypatch, profile_cache):
    config = DChatConfig(enabled=True, bot_id="bot", api_client_id="id", api_client_secret="secret")

    class DB:
        async def get(self, *args):
            return config

    calls = []

    async def lookup(config, name):
        calls.append(name)
        return {"email": "alice@example.com", "full_name": None}

    monkeypatch.setattr(dchat, "get_user_info", lookup)
    assert await dchat.display_names(DB(), ["alice"]) == {"alice": "alice"}
    assert await dchat.display_names(DB(), ["alice"]) == {"alice": "alice"}
    assert calls == ["alice"]
    async with profile_cache() as db:
        entry = (await db.scalars(select(DChatUserCache))).one()
        assert entry.profile == {"email": "alice@example.com", "full_name": None}


@pytest.mark.asyncio
async def test_cache_only_has_no_name_count_limit(monkeypatch, profile_cache):
    config = DChatConfig(enabled=True, bot_id="bot", api_client_id="id", api_client_secret="secret")
    class DB:
        async def get(self, *args):
            return config
    calls = []
    async def lookup(config, name):
        calls.append(name)
        return {"full_name": "Display " + name}
    monkeypatch.setattr(dchat, "get_user_info", lookup)
    names = ["user" + str(i) for i in range(65)]
    assert await dchat.display_names(DB(), names, cache_only=True) == {name: name for name in names}
    assert calls == []
    await dchat.display_names(DB(), names)
    calls.clear()
    assert await dchat.display_names(DB(), names, cache_only=True) == {name: "Display " + name for name in names}
    assert calls == []


def test_admin_settings_and_display_names(monkeypatch):
    async def no_send(*args, **kwargs):
        return {"success": True}
    monkeypatch.setattr(dchat, "send_text_message", no_send)
    lookups = []
    async def lookup(config, username):
        lookups.append(username)
        return {"full_name": "Display <" + username + ">", "private_field": "not-for-browser"}
    monkeypatch.setattr(dchat, "get_user_info", lookup)
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
        lookups.clear()
        data = client.get("/admin/users/data").json()
        assert set(data["display_names"]) == {row["username"] for row in data["users"]}
        client.get("/admin/workers/data")
        client.get("/admin/api-keys/data")
        client.get("/user/overview/data")
        options = client.get("/admin/options/users", params={"search": username}).json()["options"]
        assert options[0]["label"] == username
        assert lookups == []  # Initial page and picker requests never wait on D-Chat.
        response = client.get("/user/display-names", params=[("usernames", username), ("usernames", "admin")])
        assert response.status_code == 200
        assert response.json()["display_names"][username] == "Display <" + username + ">"
        assert "private_field" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/user/display-names", params=[("usernames", "admin")] * 6).status_code == 400
        assert client.get("/user/display-names", params={"usernames": "nonexistent-fixture-user"}).json() == {"display_names": {}}
        users = rendered_pages(client, "/admin/users").text
        assert "Display &lt;" + username + "&gt;" in users
        assert "/admin/users/" + username in users
        options = client.get("/admin/options/users", params={"search": username}).json()["options"]
        assert options[0]["value"] == username and options[0]["label"] == "Display <" + username + ">"
        workers = rendered_pages(client, "/admin/workers").text
        assert "Display &lt;admin&gt;" in workers
        async def seed_users():
            async with SessionLocal() as db:
                names = ["display-page-" + uuid4().hex for _ in range(35)]
                db.add_all([User(username=name, role="user", enabled=True) for name in names])
                await db.commit()
                return names
        seeded = client.portal.call(seed_users)
        seen = set()
        lookups.clear()
        first = client.get("/admin/users/data").json()
        for number in range(1, first["pagination"]["pages"] + 1):
            page = client.get("/admin/users/data", params={"page": number}).json()
            usernames = {row["username"] for row in page["users"]}
            assert len(usernames) <= 30
            assert set(page["display_names"]) == usernames
            seen.update(usernames)
        assert set(seeded) <= seen
        assert lookups == []
        user_login(client, username, password)
        assert client.get("/user/display-names", params={"usernames": username}).status_code == 200
        assert client.get("/user/display-names", params={"usernames": "admin"}).status_code == 403
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
