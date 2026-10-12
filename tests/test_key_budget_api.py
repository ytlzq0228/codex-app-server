from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from codex_gateway.main import app
from codex_gateway.config import get_settings
from codex_gateway.database import SessionLocal
from codex_gateway.models import UsageRecord
from test_admin import login, grant_admin_codex
from test_self_service import new_user, user_login
from page_helpers import rendered_pages

AJAX = {"X-Requested-With": "XMLHttpRequest"}


def test_key_budget_edit_enforcement_and_user_visibility():
    with TestClient(app) as client:
        token = login(client)
        client.portal.call(grant_admin_codex)
        assert client.post("/admin/users/"+get_settings().admin_username+"/quota",
                           data={"csrf_token":token,"amount":1},headers=AJAX).status_code == 200
        created = client.post("/admin/keys",data={"name":"budget-"+uuid4().hex[:8],
                              "csrf_token":token},headers=AJAX)
        assert created.status_code == 200
        key_id, secret = created.json()["key_id"], created.json()["secret"]
        headers = {"Authorization":"Bearer "+secret, "anthropic-version":"2023-06-01"}
        data = client.get("/admin/api-keys/data").json()
        assert data["key_budgets"][key_id]["limit"] == "3000.00"
        endpoint = "/admin/keys/"+key_id+"/budget"
        for value in ["-1","NaN","Infinity","1.001"]:
            assert client.post(endpoint,data={"csrf_token":token,"monthly_limit_usd":value},
                               headers=AJAX).status_code == 400
        assert client.post(endpoint,data={"csrf_token":"bad","monthly_limit_usd":"5"},
                           headers=AJAX).status_code == 403

        async def add_usage():
            async with SessionLocal() as db:
                db.add(UsageRecord(api_key_id=UUID(key_id),request_id=str(uuid4()),model="test",
                       status_code=499,error_code="request_interrupted",cost_usd=Decimal("12.50"),
                       created_at=datetime.now(timezone.utc)))
                await db.commit()
        client.portal.call(add_usage)
        assert client.post(endpoint,data={"csrf_token":token,"monthly_limit_usd":"12.50"},
                           headers=AJAX).status_code == 200
        for path, body in [
            ("/v1/responses", {"model":"gpt-6-sol","input":"hi"}),
            ("/v1/chat/completions", {"model":"gpt-6-sol","messages":[{"role":"user","content":"hi"}],"stream":True}),
            ("/v1/messages", {"model":"claude-sonnet-4-6","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}),
            ("/v1beta/models/gemini-3.1-flash-lite-preview:generateContent", {"contents":[{"parts":[{"text":"hi"}]}]}),
        ]:
            response=client.post(path,json=body,headers=headers)
            assert response.status_code == 429,response.text
        assert client.get("/v1/models",headers=headers).status_code == 200
        assert client.post(endpoint,data={"csrf_token":token,"monthly_limit_usd":"3000"},
                           headers=AJAX).status_code == 200
        response=client.post("/v1/responses",json={"model":"gpt-6-sol","input":"budget restored"},headers=headers)
        assert response.status_code == 200,response.text

        expected_used=client.get("/admin/api-keys/data").json()["key_budgets"][key_id]["used"]
        username,password=new_user(client,token)
        assert client.post("/admin/keys/"+key_id+"/owner",
                           data={"csrf_token":token,"username":username},headers=AJAX).status_code == 200
        admin_html=rendered_pages(client,"/admin/api-keys").text
        assert 'data-owner-edit' in admin_html and 'key-budget' in admin_html
        user_token=user_login(client,username,password)
        data=client.get("/user/account/data").json()
        assert data["key_budgets"][key_id]["used"] == expected_used
        assert {key["id"] for key in data["keys"]} == {key_id}
        assert client.post(endpoint,data={"csrf_token":user_token,"monthly_limit_usd":"9999"},
                           headers=AJAX).status_code == 403
        assert expected_used in rendered_pages(client,"/user/account").text


def test_legacy_budget_migration_is_idempotent():
    from sqlalchemy import text
    from codex_gateway.models import Base
    from codex_gateway.migrations import upgrade
    with TestClient(app) as client:
        async def migrate():
            async with SessionLocal() as db:
                schema = "budget_migration_" + uuid4().hex
                await db.execute(text("CREATE SCHEMA " + schema))
                await db.execute(text("SET LOCAL search_path TO " + schema))
                conn = await db.connection()
                await conn.run_sync(Base.metadata.create_all)
                await db.execute(text("ALTER TABLE api_keys DROP COLUMN monthly_limit_usd"))
                legacy_id = uuid4()
                await db.execute(text("INSERT INTO api_keys (id,name,prefix,key_hash,enabled,scheduling_mode) VALUES (:id,'legacy','legacy','hash',true,'pooled')"), {"id":legacy_id})
                await upgrade(conn)
                assert await db.scalar(text("SELECT monthly_limit_usd FROM api_keys WHERE id=:id"),{"id":legacy_id}) == Decimal("3000")
                await db.execute(text("UPDATE api_keys SET monthly_limit_usd=123.45"))
                await upgrade(conn)
                assert await db.scalar(text("SELECT monthly_limit_usd FROM api_keys WHERE id=:id"),{"id":legacy_id}) == Decimal("123.45")
                await db.rollback()  # Includes temporary schema creation.
        client.portal.call(migrate)
