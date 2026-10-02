from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from codex_gateway.config import get_settings
from codex_gateway.database import SessionLocal
from codex_gateway.main import app
from codex_gateway.models import Worker, WorkerStatus, UsageRecord
from test_self_service import AJAX, admin_login, new_user, user_login


def test_claude_admin_creation_grants_forwarding_and_revocation(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "allowed_models", "gpt-6-sol,claude-product")
    monkeypatch.setattr(settings, "model_providers", "claude-product:claude")
    created = []
    original = httpx.AsyncClient.post

    async def manager(client, url, **kwargs):
        if str(url) == settings.manager_url + "/workers":
            created.append(kwargs["json"])
            return httpx.Response(201, json={"name": kwargs["json"]["name"],
                                            "endpoint": "http://claude-product:4500"})
        return await original(client, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", manager)
    with TestClient(app) as client:
        token = admin_login(client)
        owner, password = new_user(client, token)
        name = "claude-product-" + uuid4().hex[:12]
        response = client.post("/admin/workers", data={
            "name": name, "provider": "claude", "csrf_token": token}, headers=AJAX)
        assert response.status_code == 200, response.text
        assert created == [{"name": name, "provider": "claude"}]

        async def ready():
            async with SessionLocal() as db:
                worker = await db.scalar(select(Worker).where(Worker.name == name))
                assert worker.provider == "claude"
                assert worker.endpoint == "http://claude-product:4500"
                worker.status = WorkerStatus.ready
                await db.commit()
                return worker.id

        worker_id = client.portal.call(ready)
        user_token = user_login(client, owner, password)
        response = client.post("/user/account/key", data={"csrf_token": user_token}, headers=AJAX)
        assert response.status_code == 200, response.text
        raw = response.json()["secret"]
        key_id = UUID(response.json()["key_id"])
        headers = {"Authorization": "Bearer " + raw, "anthropic-version": "2023-06-01"}
        paths = [
            ("/v1/responses", {"input": "hello"}),
            ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hello"}]}),
            ("/v1/messages", {"max_tokens": 1024, "messages": [{"role": "user", "content": "hello"}]}),
            ("/v1/messages/count_tokens", {"messages": [{"role": "user", "content": "hello"}]}),
        ]
        for path, body in paths:
            assert client.post(path, headers=headers, json={"model": "claude-product", **body}).status_code == 403

        token = admin_login(client)
        grant = f"/admin/users/{owner}/providers"
        assert client.post(grant, data={"csrf_token": token, "claude": "true"}, headers=AJAX).status_code == 200
        assert [m["id"] for m in client.get("/v1/models", headers=headers).json()["data"]] == ["claude-product"]
        for path, body in paths:
            for stream in ([False, True] if not path.endswith("count_tokens") else [False]):
                payload = {"model": "claude-product", **body}
                if not path.endswith("count_tokens"):
                    payload["stream"] = stream
                response = client.post(path, headers=headers, json=payload)
                assert response.status_code == 200, response.text
                if not path.endswith("count_tokens"):
                    assert response.headers["x-gateway-generation-policy"] == "worker-defaults"

        async def audit():
            async with SessionLocal() as db:
                rows = (await db.scalars(select(UsageRecord).where(
                    UsageRecord.api_key_id == key_id, UsageRecord.worker_id == worker_id))).all()
                assert len(rows) == 6
                assert all(row.provider == "claude" for row in rows)
        client.portal.call(audit)
        assert client.post(grant, data={"csrf_token": token}, headers=AJAX).status_code == 200
        assert client.get("/v1/models", headers=headers).json()["data"] == []
        for path, body in paths:
            assert client.post(path, headers=headers, json={"model": "claude-product", **body}).status_code == 403

