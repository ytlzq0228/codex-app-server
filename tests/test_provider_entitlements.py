from datetime import datetime, timezone
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import select
from codex_gateway.main import app
from codex_gateway.config import get_settings
from codex_gateway.database import SessionLocal
from codex_gateway.models import User, Worker, WorkerStatus, ApiKey, UsageRecord
from codex_gateway.security import generate_api_key, hash_api_key
from codex_gateway.quota import quota_summary, credited_workers
from codex_gateway.subscriptions import subscription_summary


def test_owner_provider_entitlements_and_cross_provider_credits(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "model_providers", "gemini-entitlement:gemini")
    monkeypatch.setattr(settings, "allowed_models", "gpt-6-sol,gemini-entitlement")
    owner = "entitlement-" + uuid4().hex[:12]
    workers = []
    keys = []

    async def seed():
        async with SessionLocal() as db:
            db.add(User(username=owner, enabled=True))
            await db.flush()
            for _ in range(2):
                ident = uuid4()
                workers.append(ident)
                db.add(Worker(id=ident, owner_username=owner, name=ident.hex, container_name=ident.hex,
                    provider="codex", endpoint="ws://test", enabled=True, status=WorkerStatus.ready,
                    auth_mode="chatgpt", plan_type="plus", account_email="same@example.test",
                    account_checked_at=datetime.now(timezone.utc)))
            for _ in range(2):
                raw, prefix = generate_api_key()
                keys.append(raw)
                db.add(ApiKey(name="test", prefix=prefix, key_hash=hash_api_key(raw,settings.key_pepper.get_secret_value()),
                              owner_username=owner, enabled=True))
            await db.commit()

    async def change(provider, plan="gcp-ge-plus-tier", removed=False):
        async with SessionLocal() as db:
            w = await db.get(Worker, workers[-1])
            w.provider = provider
            w.auth_mode = "google-subscription" if provider == "gemini" else "chatgpt"
            w.plan_type = plan
            if removed: w.endpoint = "removed://worker"
            await db.commit()

    async def inspect():
        async with SessionLocal() as db:
            return await quota_summary(db,owner), await credited_workers(db,owner)

    with TestClient(app) as client:
        client.portal.call(seed)
        q, (credited, duplicates) = client.portal.call(inspect)
        assert q["contributed"] == 1 and len(duplicates) == 1
        for raw in keys:
            headers = {"Authorization": "Bearer "+raw}
            assert [m["id"] for m in client.get("/v1/models",headers=headers).json()["data"]] == ["gpt-6-sol"]
            for path, payload in [("/v1/chat/completions", {"messages":[{"role":"user","content":"hi"}]}),
                                  ("/v1/responses", {"input":"hi"})]:
                r=client.post(path,headers=headers,json={"model":"gemini-entitlement",**payload})
                assert r.status_code == 403 and r.json()["error"]["code"] == "provider_not_allowed"
        client.portal.call(change,"gemini")
        q, (credited, duplicates) = client.portal.call(inspect)
        assert q["contributed"] == 2 and not duplicates
        for raw in keys:
            headers={"Authorization":"Bearer "+raw}
            assert len(client.get("/v1/models",headers=headers).json()["data"]) == 2
            assert client.get("/v1/models/gemini-entitlement",headers=headers).status_code == 200
            r=client.post("/v1/responses",headers=headers,json={"model":"gemini-entitlement","input":"hi","temperature":1})
            assert r.status_code == 400 and r.json()["error"]["param"] == "temperature"
        client.portal.call(change,"gemini","free-tier")
        assert client.portal.call(inspect)[0]["contributed"] == 1
        client.portal.call(change,"gemini","gcp-ge-plus-tier",True)
        assert len(client.get("/v1/models",headers=headers).json()["data"]) == 1

        async def audit():
            async with SessionLocal() as db:
                row=await db.scalar(select(UsageRecord).where(UsageRecord.owner_username==owner).order_by(UsageRecord.created_at.desc()))
                assert row.provider == "gemini"
        client.portal.call(audit)


def test_subscription_namespaced_by_provider():
    unique="same-plan-"+uuid4().hex[:12]
    async def check():
        async with SessionLocal() as db:
            for provider in ("codex","gemini"):
                ident=uuid4().hex
                db.add(Worker(name=ident,container_name=ident,provider=provider,endpoint="http://test",
                              auth_mode="chatgpt" if provider=="codex" else "google-subscription",plan_type=unique))
            await db.flush()
            summary=await subscription_summary(db)
            rows={r["name"]:r for r in summary["rows"]}
            assert rows[unique]["count"] == rows["gemini:"+unique]["count"] == 1
            assert rows[unique]["label"].startswith("OpenAI")
            assert rows["gemini:"+unique]["label"].startswith("Gemini")
            await db.rollback()
    with TestClient(app) as client:
        client.portal.call(check)


def test_gemini_web_logout_invalidates_credit_and_sessions(monkeypatch):
    import httpx
    from uuid import UUID
    from codex_gateway.models import ResponseBinding
    from test_self_service import admin_login, user_login, AJAX
    from test_quota_workers import create_person
    import codex_gateway.gemini_backend as gemini
    original_post = httpx.AsyncClient.post
    calls = []
    async def post(client, url, **kwargs):
        if str(url) == get_settings().manager_url + "/workers":
            calls.append(kwargs["json"])
            return httpx.Response(201, json={"name":kwargs["json"]["name"],"endpoint":"http://gemini:4500"})
        return await original_post(client,url,**kwargs)
    async def rpc(endpoint, settings, path, payload=None):
        calls.append(path)
        if path == "/login/logout": return {"logged_in":False,"account":None}
        if path == "/rate-limits": return {"buckets":[],"available":False,"message":"企业套餐未返回数值额度"}
        return {"session_id":"new-session","logged_in":False}
    monkeypatch.setattr(httpx.AsyncClient,"post",post)
    monkeypatch.setattr(gemini,"worker_rpc",rpc)
    with TestClient(app) as client:
        owner,password=create_person(client)
        token=user_login(client,owner,password)
        r=client.post("/user/workers",data={"provider":"gemini","csrf_token":token},headers=AJAX)
        assert r.status_code == 200,r.text
        ident=UUID(r.json()["worker_id"])
        assert calls[-1]["provider"] == "gemini"
        async def ready():
            async with SessionLocal() as db:
                w=await db.get(Worker,ident)
                w.auth_mode="google-subscription"
                w.plan_type="gcp-ge-plus-tier"
                w.account_email="web@example.test"
                w.account_checked_at=datetime.now(timezone.utc)
                w.status=WorkerStatus.ready
                await db.commit()
        client.portal.call(ready)
        r=client.post("/user/account/key",data={"name":"web-key","csrf_token":token},headers=AJAX)
        assert r.status_code == 200,r.text
        key_id=UUID(r.json()["key_id"])
        response_id="resp_"+uuid4().hex
        async def bind():
            async with SessionLocal() as db:
                db.add(ResponseBinding(response_id=response_id,api_key_id=key_id,worker_id=ident,
                    provider="gemini",thread_id="native-thread"))
                await db.commit()
        client.portal.call(bind)
        path=f"/user/workers/{ident}/gemini-login/"
        assert client.post(path+"logout",data={"csrf_token":"bad"},headers=AJAX).status_code == 403
        assert client.post(f"/admin/workers/{ident}/gemini-login/logout",data={"csrf_token":token},headers=AJAX).status_code == 403
        r=client.post(f"/user/workers/{ident}/rate-limits",data={"csrf_token":token},headers=AJAX)
        assert r.status_code == 200 and r.json()["available"] is False
        assert client.post(path+"logout",data={"csrf_token":token},headers=AJAX).status_code == 200
        async def inspect():
            async with SessionLocal() as db:
                w=await db.get(Worker,ident)
                assert not w.auth_mode and w.execution_generation == 1
                assert not (await db.get(ApiKey,key_id)).enabled
                assert (await db.get(ResponseBinding,response_id)).status != "active"
                assert (await quota_summary(db,owner))["contributed"] == 0
        client.portal.call(inspect)
        assert client.post(path+"start",data={"csrf_token":token},headers=AJAX).json()["session_id"] == "new-session"
        # Admin creation also passes provider to the manager.
        token=admin_login(client)
        r=client.post("/admin/workers",data={"name":"gemini-"+uuid4().hex[:10],"provider":"gemini","csrf_token":token},headers=AJAX)
        assert r.status_code == 200 and calls[-1]["provider"] == "gemini"


def test_admin_manual_provider_grants_and_revocation(monkeypatch):
    from test_self_service import admin_login, user_login, AJAX
    from test_quota_workers import create_person
    from codex_gateway.providers import allowed_providers
    settings = get_settings()
    monkeypatch.setattr(settings, 'allowed_models', 'gpt-6-sol,gemini-manual')
    monkeypatch.setattr(settings, 'model_providers', 'gemini-manual:gemini')
    async def seed_key(owner):
        raw, prefix = generate_api_key()
        async with SessionLocal() as db:
            db.add(ApiKey(name='manual', prefix=prefix, owner_username=owner, enabled=True,
                          key_hash=hash_api_key(raw, settings.key_pepper.get_secret_value())))
            ident = uuid4().hex
            db.add(Worker(name=ident, container_name=ident, owner_username=owner,
                          provider='codex', endpoint='ws://test', status=WorkerStatus.offline))
            await db.commit()
        return raw
    with TestClient(app) as client:
        owner, password = create_person(client)
        admin, _ = create_person(client, role='admin')
        token = admin_login(client)
        raw = client.portal.call(seed_key, owner)
        headers = {'Authorization': 'Bearer '+raw}
        path = f'/admin/users/{owner}/providers'
        def models():
            return [x['id'] for x in client.get('/v1/models', headers=headers).json()['data']]
        assert models() == ['gpt-6-sol']
        assert client.post(path, data={'csrf_token': 'wrong', 'gemini': 'true'}, headers=AJAX).status_code == 403
        assert client.post(path, data={'csrf_token': token, 'gemini': 'true'}, headers=AJAX).status_code == 200
        assert models() == ['gpt-6-sol', 'gemini-manual']
        assert 'Provider 权限' in client.get('/admin/users').text
        assert client.post(path, data={'csrf_token': token}, headers=AJAX).status_code == 200
        assert models() == ['gpt-6-sol']  # Automatic Worker entitlement is preserved.
        assert client.post('/v1/responses', headers=headers, json={'model':'gemini-manual','input':'hi'}).status_code == 403
        user_token = user_login(client, owner, password)
        assert client.post(path, data={'csrf_token':user_token,'gemini':'true'}, headers=AJAX).status_code == 403
        # A regular administrator cannot change another administrator's grants.
        async def become_admin():
            async with SessionLocal() as db:
                user = await db.get(User, owner); user.role = 'admin'; await db.commit()
        client.portal.call(become_admin)
        assert client.post(f'/admin/users/{admin}/providers', data={'csrf_token':user_token,'gemini':'true'}, headers=AJAX).status_code == 403
