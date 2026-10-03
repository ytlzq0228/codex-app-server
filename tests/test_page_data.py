from decimal import Decimal
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import select

from codex_gateway.main import app
from codex_gateway.database import SessionLocal
from codex_gateway.models import ApiKey, User, UsageRecord
from codex_gateway.page_data import display_data
from test_self_service import admin_login, user_login, new_user, AJAX


def test_page_json_bounds_secrets_and_options():
    prefix = "json-" + uuid4().hex
    async def seed():
        async with SessionLocal() as db:
            for index in range(35):
                db.add(ApiKey(name=f"{prefix}-{index}", prefix=(prefix[5:20]+str(index)),
                              key_hash=uuid4().hex*2))
            await db.commit()
    with TestClient(app) as client:
        admin_login(client)
        client.portal.call(seed)
        shell = client.get("/admin/api-keys")
        assert "data-json-page" in shell.text and prefix not in shell.text
        first = client.get("/admin/api-keys/data")
        assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
        data = first.json()
        assert len(data["keys"]) == 30
        second = client.get("/admin/api-keys/data?page=2").json()
        assert not {r["id"] for r in data["keys"]} & {r["id"] for r in second["keys"]}
        assert client.get("/admin/api-keys/data?page=0").status_code == 422
        assert client.get("/admin/api-keys/data?page=oops").status_code == 422
        last = client.get("/admin/api-keys/data?page=9999999").json()
        assert last["pagination"]["page"] == last["pagination"]["pages"]
        for response in (data, second, last):
            assert len(response["keys"]) <= 30
            assert all("key_hash" not in key for key in response["keys"])
            assert "password_hash" not in response["identity"]
        options = client.get("/admin/options/users?search=definitely-absent-"+prefix).json()
        assert options["options"] == []
        assert len(client.get("/admin/options/users").json()["options"]) <= 30


def test_json_owner_scope_and_initial_password():
    with TestClient(app) as client:
        token=admin_login(client)
        alice,password=new_user(client,token)
        bob,bob_password=new_user(client,token)
        client.post("/auth/login",data={"username":alice,"password":password},follow_redirects=False)
        assert client.get("/user/account/data").status_code == 200
        assert client.get("/admin/users/data",follow_redirects=False).status_code in (303,403)
        user_login(client,alice,password)
        request_id="json-"+uuid4().hex
        async def seed():
            async with SessionLocal() as db:
                db.add(UsageRecord(request_id=request_id,owner_username=alice,
                    logical_conversation_id=request_id,endpoint="responses",model="test",
                    status_code=200,input_tokens=1250000,output_tokens=1,
                    request_params={"input":"private fixture"},cost_usd=Decimal("0.01")))
                await db.commit()
        client.portal.call(seed)
        history=client.get("/user/usage/data",params={"q":request_id}).json()["history"]
        assert history["total"] == 1 and "requests" not in history["groups"][0]
        params={"conversation":request_id,"key_id":"development","endpoint":"responses"}
        response=client.get("/user/usage/requests",params=params)
        assert response.json()["total"] == 1
        assert "private fixture" not in response.text
        detail=client.get("/user/usage/"+request_id+"/data")
        assert detail.status_code == 200 and "private fixture" in detail.text
        user_login(client,bob,bob_password)
        assert client.get("/user/usage/data",params={"q":request_id}).json()["history"]["total"] == 0
        assert client.get("/user/usage/requests",params=params).json()["total"] == 0
        assert client.get("/user/usage/"+request_id+"/data").status_code == 404
        assert client.get("/admin/options/users").status_code == 403


def test_static_layout_is_in_initial_html():
    pages = {
        "/admin": "运行控制台", "/admin/api-keys": "API Keys",
        "/admin/workers": "Worker 管理", "/admin/sessions": "Key 活动会话",
        "/admin/users": "用户管理", "/admin/finance": "价格配置",
        "/admin/reports": "财务报表", "/admin/google": "Google 登录配置",
        "/user/overview": "自助服务概览", "/user/account": "我的账户",
        "/user/workers": "贡献 Worker", "/user/usage": "用量详单",
        "/user/debug": "查询与调试", "/user/usage/shell-placeholder": "请求详情",
    }
    with TestClient(app) as client:
        admin_login(client)
        for path, heading in pages.items():
            response = client.get(path)
            assert response.status_code == 200, path
            assert f"<h1>{heading}</h1>" in response.text, path
            assert 'data-page-loading' in response.text
            assert '<section' in response.text
            assert '正在加载页面数据' not in response.text
            assert 'data-page-retry hidden>重试' in response.text
            # Data-dependent interactions initialize only after JSON rendering.
            assert '<script defer src="/static/portal.js">' not in response.text
            assert '<script defer src="/static/admin.js">' not in response.text
