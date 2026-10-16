from uuid import UUID, uuid4
from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from codex_gateway.main import app
from codex_gateway.database import SessionLocal
from codex_gateway.models import ApiKey, ServiceKeyEvent, Base
from codex_gateway.migrations import upgrade, bootstrap_users
from codex_gateway.config import get_settings
from test_self_service import admin_login, new_user, user_login, csrf
from page_helpers import rendered_pages

AJAX = {"X-Requested-With": "XMLHttpRequest"}


def post(client, path, token, **data):
    return client.post(path, data={"csrf_token": token, **data}, headers=AJAX)


def test_service_key_lifecycle_and_isolation():
    with TestClient(app) as client:
        admin = admin_login(client)
        owner, password = new_user(client, admin)
        target, _ = new_user(client, admin)
        assert post(client, f"/admin/users/{owner}/quota", admin, quota_granted=0).status_code == 200
        assert post(client, f"/admin/users/{target}/quota", admin, quota_granted=0).status_code == 200
        user = user_login(client, owner, password)
        assert post(client, "/user/account/key", user, name="ordinary").status_code == 409
        assert post(client, "/user/service-key-requests", "bad", name="test", reason="test").status_code == 403
        application = post(client, "/user/service-key-requests", user, name="service-"+uuid4().hex[:8], reason="automation")
        assert application.status_code == 200, application.text
        request_id = application.json()["request_id"]
        review = f"/admin/service-key-requests/{request_id}/review"
        assert post(client, review, user, decision="approve").status_code == 403
        assert client.get("/user/account/data").json()["keys"] == []
        admin = admin_login(client)
        assert post(client, review, admin, decision="reject", note="needs review").status_code == 200
        html = rendered_pages(client, "/admin/service-keys?tab=requests").text
        assert "needs review" in html and "重新审批通过" in html
        approved = post(client, review, admin, decision="approve")
        assert approved.status_code == 200, approved.text
        key_id = approved.json()["key_id"]
        assert post(client, review, admin, decision="approve").status_code == 409
        assert post(client, review, admin, decision="reject", note="late").status_code == 409
        client.post("/auth/login", data={"username":owner,"password":"changed-"+password})
        user = csrf(client)
        data = client.get("/user/account/data").json()
        key = next(k for k in data["keys"] if k["id"] == key_id)
        assert key["key_type"] == "project_service_key"
        assert Decimal(key["monthly_limit_usd"]) == Decimal("3000")
        assert data["quota"]["used"] == 0
        assert "project_service_key" in rendered_pages(client, "/user/account").text
        rotated = post(client, f"/user/account/keys/{key_id}/rotate", user)
        secret = rotated.json()["secret"]
        headers = {"Authorization":"Bearer "+secret}
        assert client.get("/v1/models", headers=headers).json()["data"] == []
        body = {"model":"gpt-6-sol","input":"hello"}
        assert client.post("/v1/responses", headers=headers, json=body).status_code == 403
        assert post(client, f"/admin/service-keys/{key_id}/providers", user, codex="true").status_code == 403
        assert post(client, f"/admin/keys/{key_id}/edit", user, name="changed", scheduling_mode="pooled").status_code == 403
        assert post(client, f"/user/account/keys/{key_id}/toggle", user).status_code == 200
        assert post(client, f"/user/account/keys/{key_id}/toggle", user).status_code == 200
        admin = admin_login(client)
        assert post(client, f"/admin/service-keys/{key_id}/providers", admin, codex="true").status_code == 200
        assert client.post("/v1/responses", headers=headers, json=body).status_code == 200
        assert post(client, f"/admin/users/{owner}/quota", admin, quota_granted=0).status_code == 200
        assert client.post("/v1/responses", headers=headers, json=body).status_code == 200
        assert post(client, f"/admin/users/{owner}", admin, role="user", enabled="false").status_code == 200
        assert client.post("/v1/responses", headers=headers, json=body).status_code == 200
        assert post(client, f"/admin/keys/{key_id}/toggle", admin).status_code == 200
        assert client.get("/v1/models", headers=headers).status_code == 401
        assert post(client, f"/admin/users/{owner}", admin, role="user", enabled="true").status_code == 200
        client.post("/auth/login", data={"username":owner,"password":"changed-"+password})
        user = csrf(client)
        assert post(client, f"/user/account/keys/{key_id}/toggle", user).status_code == 403
        rotated = post(client, f"/user/account/keys/{key_id}/rotate", user)
        secret = rotated.json()["secret"]
        headers = {"Authorization":"Bearer "+secret}
        assert client.get("/v1/models", headers=headers).status_code == 401
        admin = admin_login(client)
        assert post(client, f"/admin/users/{owner}/delete", admin).status_code == 200
        assert client.get("/v1/models", headers=headers).status_code == 401

        async def check_orphan():
            async with SessionLocal() as db:
                await bootstrap_users(db, get_settings())
                await db.commit()
                key = await db.get(ApiKey, UUID(key_id))
                assert key.owner_username is None
                assert key.deleted_at is None
        client.portal.call(check_orphan)
        assert "归属失效" in rendered_pages(client, "/admin/service-keys").text
        assert post(client, f"/admin/keys/{key_id}/owner", admin, username=target).status_code == 200
        assert client.get("/v1/models", headers=headers).status_code == 401  # Admin suspension survives transfer.
        assert post(client, f"/admin/keys/{key_id}/toggle", admin).status_code == 200
        assert client.post("/v1/responses", headers=headers, json=body).status_code == 200  # Same secret.
        assert post(client, f"/admin/service-keys/{key_id}/providers", admin).status_code == 200
        assert client.post("/v1/responses", headers=headers, json=body).status_code == 403
        async def check_events():
            async with SessionLocal() as db:
                events = list(await db.scalars(select(ServiceKeyEvent.action).where(ServiceKeyEvent.key_id == UUID(key_id))))
                assert {"approved", "transferred", "owner_deleted", "providers_updated", "admin_disabled", "admin_enabled"} <= set(events)
        client.portal.call(check_events)


def test_service_migration_preserves_types_and_grants():
    with TestClient(app) as client:
        async def migrate():
            async with SessionLocal() as db:
                schema = "service_migration_" + uuid4().hex
                await db.execute(text("CREATE SCHEMA " + schema))
                await db.execute(text("SET LOCAL search_path TO " + schema))
                conn = await db.connection()
                await conn.run_sync(Base.metadata.create_all)
                for column in ("key_type", "provider_grants", "admin_disabled"):
                    await db.execute(text("ALTER TABLE api_keys DROP COLUMN " + column))
                legacy = uuid4()
                await db.execute(text("INSERT INTO api_keys (id,name,prefix,key_hash,enabled,scheduling_mode) VALUES (:id,'legacy','legacy','hash',true,'pooled')"), {"id":legacy})
                await upgrade(conn)
                assert await db.scalar(text("SELECT key_type FROM api_keys WHERE id=:id"), {"id":legacy}) == "general_user_key"
                await db.execute(text("""UPDATE api_keys SET key_type='project_service_key', provider_grants='["claude"]', admin_disabled=true"""))
                await upgrade(conn)
                row = (await db.execute(text("SELECT key_type, provider_grants, admin_disabled FROM api_keys"))).one()
                assert row == ("project_service_key", ["claude"], True)
                await db.rollback()
        client.portal.call(migrate)


def test_concurrent_approval_creates_one_key_and_user_cannot_cross_owners():
    from concurrent.futures import ThreadPoolExecutor
    with TestClient(app) as client:
        admin = admin_login(client)
        owner, password = new_user(client, admin)
        other, other_password = new_user(client, admin)
        user = user_login(client, owner, password)
        application = post(client, "/user/service-key-requests", user, name="race", reason="automation").json()["request_id"]
        admin = admin_login(client)
        def approve():
            return post(client, f"/admin/service-key-requests/{application}/review", admin, decision="approve")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: approve(), range(2)))
        assert sorted(r.status_code for r in results) == [200, 409]
        key_id = next(r.json()["key_id"] for r in results if r.status_code == 200)
        user = user_login(client, other, other_password)
        for action in ["rotate", "toggle", "delete", "name"]:
            assert post(client, f"/user/account/keys/{key_id}/{action}", user, name="stolen").status_code == 404
        client.post("/auth/login", data={"username":owner,"password":"changed-"+password})
        user = csrf(client)
        assert post(client, f"/user/account/keys/{key_id}/name", user, name="renamed", scheduling_mode="pinned", key_type="general_user_key", codex="true").status_code == 200
        data = client.get("/user/account/data").json()["keys"][0]
        assert (data["name"], data["key_type"], data["scheduling_mode"], data["provider_grants"]) == ("renamed", "project_service_key", "pooled", [])
        secret = post(client, f"/user/account/keys/{key_id}/rotate", user).json()["secret"]
        assert post(client, f"/user/account/keys/{key_id}/delete", user).status_code == 200
        assert client.get("/v1/models", headers={"Authorization":"Bearer "+secret}).status_code == 401
