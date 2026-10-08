import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from codex_gateway.config import get_settings
from codex_gateway.main import app
from codex_gateway.model_mapping import resolve_model
from codex_gateway.schemas import ResponseRequest
from test_self_service import AJAX, admin_login, new_user, user_login


@pytest.mark.asyncio
async def test_mapping_priority_and_single_replacement(monkeypatch):
    monkeypatch.setattr(get_settings(), "model_aliases", "public:default,actual:other")
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(upstream_model="actual")))
    assert await resolve_model(db, "public") == "actual"
    db.get.return_value = None
    assert await resolve_model(db, "public") == "public"
    assert await resolve_model(db, "unmapped") == "unmapped"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gemini", "claude"])
async def test_adapter_uses_mapping_without_changing_public_model(monkeypatch, provider):
    from codex_gateway.backend import BackendTarget
    from codex_gateway.gemini_backend import GeminiAdapter
    from codex_gateway.claude_backend import ClaudeAdapter
    settings = get_settings()
    monkeypatch.setattr(settings, "model_providers", f"public:{provider}")
    received = []
    async def handle(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, text='{"thread_id":"thread","delta":"ok"}\n{"thread_id":"thread","done":true}\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    request = ResponseRequest(model="public", input="hello")
    request._upstream_model = "actual"
    adapter = (GeminiAdapter if provider == "gemini" else ClaudeAdapter)(settings)
    target = BackendTarget("key", "http://worker", "/workspace", provider=provider)
    await adapter.complete(request, target)
    assert received[0]["model"] == "actual"
    assert request.model == "public"
    assert "_upstream_model" not in request.model_dump()


def test_admin_mapping_crud_permissions_and_rendering(monkeypatch):
    from page_helpers import rendered_pages
    settings = get_settings()
    monkeypatch.setattr(settings, "allowed_models", "gemini-3.1-flash-lite-preview,gpt-6-sol")
    monkeypatch.setattr(settings, "model_providers", "gemini-3.1-flash-lite-preview:gemini")
    public = "gemini-3.1-flash-lite-preview"
    actual = "gemini-3.6-flash-high"
    with TestClient(app) as client:
        assert client.post("/admin/model-mappings", data={"model": public, "upstream_model": actual, "csrf_token": "bad"}, headers=AJAX).status_code in (401, 403)
        token = admin_login(client)
        def save(value, csrf=token, model=public):
            return client.post("/admin/model-mappings", data={"csrf_token": csrf, "model": model, "upstream_model": value}, headers=AJAX)
        assert save(actual, csrf="bad").status_code == 403
        try:
            assert save(actual).status_code == 200
            assert actual in rendered_pages(client, "/admin/finance").text
            rows = client.get("/admin/finance/data").json()["mapping_rows"]
            assert next(row for row in rows if row["model"] == public)["upstream_model"] == actual
            assert save("gpt-6-sol").status_code == 400
            assert save("bad model").status_code == 400
            assert save(actual, model="unknown").status_code == 400
            alias = "gemini-mapping-new"
            assert save(actual, model=alias).status_code == 200
            try:
                auth = {"Authorization": "Bearer cag_dev_local"}
                assert client.get("/v1/models/"+alias, headers=auth).status_code == 200
                assert alias in {row["id"] for row in client.get("/v1/models", headers=auth).json()["data"]}
            finally:
                assert save("", model=alias).status_code == 200
            assert save("gemini-updated").status_code == 200
            assert save("").status_code == 200
            rows = client.get("/admin/finance/data").json()["mapping_rows"]
            assert all(row["model"] != public for row in rows)
            username, password = new_user(client, token)
            user_token = user_login(client, username, password)
            assert save(actual, csrf=user_token).status_code == 403
        finally:
            token = admin_login(client)
            assert save("", csrf=token).status_code == 200

@pytest.mark.parametrize("endpoint", ["/v1/responses", "/v1/chat/completions"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mapped", [False, True])
def test_api_mapping_retains_public_response(monkeypatch, endpoint, stream, mapped):
    from codex_gateway.backend import MockBackend
    monkeypatch.setattr(get_settings(), "model_aliases", "gpt-6-sol:legacy-upstream")
    original = MockBackend.complete
    seen = []
    async def complete(self, request, target):
        seen.append((request.model, request._upstream_model))
        return await original(self, request, target)
    monkeypatch.setattr(MockBackend, "complete", complete)
    with TestClient(app) as client:
        token = admin_login(client)
        data = {"csrf_token": token, "model": "gpt-6-sol", "upstream_model": "actual-codex" if mapped else ""}
        assert client.post("/admin/model-mappings", data=data, headers=AJAX).status_code == 200
        try:
            body = {"model": "gpt-6-sol", "stream": stream}
            body.update({"input": "hello"} if endpoint.endswith("responses") else {"messages": [{"role": "user", "content": "hello"}]})
            result = client.post(endpoint, json=body, headers={"Authorization": "Bearer cag_dev_local"})
            assert result.status_code == 200, result.text
            assert seen == [("gpt-6-sol", "actual-codex" if mapped else "gpt-6-sol")]
            if stream:
                events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
                models = [event.get("model") or event.get("response", {}).get("model") for event in events]
                assert "gpt-6-sol" in models
                assert "actual-codex" not in models
            else:
                assert result.json()["model"] == "gpt-6-sol"
        finally:
            data["upstream_model"] = ""
            assert client.post("/admin/model-mappings", data=data, headers=AJAX).status_code == 200

@pytest.mark.asyncio
async def test_remote_forwarding_preserves_resolved_model(monkeypatch):
    from codex_gateway.cluster import remote_stream
    from codex_gateway.backend import BackendTarget
    received = []
    async def handle(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, text='{"thread_id":"thread","done":true}\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    request = ResponseRequest(model="gpt-6-sol", input="hello")
    request._upstream_model = "actual-codex"
    target = BackendTarget("key", "ws://worker", "/workspace")
    events = [event async for event in remote_stream("http://owner", request, target, get_settings())]
    assert events[-1].done
    assert received[0]["upstream_model"] == "actual-codex"
    assert received[0]["request"]["model"] == "gpt-6-sol"
