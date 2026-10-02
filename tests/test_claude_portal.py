from fastapi.testclient import TestClient

from codex_gateway.config import get_settings
from codex_gateway.main import app
from test_self_service import AJAX, admin_login, new_user, user_login


def test_debug_and_account_use_owner_provider_permissions(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "allowed_models", "gpt-6-sol,claude-test,gemini-test")
    monkeypatch.setattr(settings, "model_providers", "claude-test:claude,gemini-test:gemini")
    with TestClient(app) as client:
        token = admin_login(client)
        alice, password = new_user(client, token)
        bob, bob_password = new_user(client, token)
        response = client.post(f"/admin/users/{alice}/providers",
                               data={"csrf_token": token, "claude": "true"}, headers=AJAX)
        assert response.status_code == 200
        user_login(client, alice, password)
        page = client.get("/user/debug")
        assert page.status_code == 200
        assert 'value="/v1/messages"' in page.text
        assert 'value="/v1/messages/count_tokens"' in page.text
        assert 'value="claude-test" data-provider="claude"' in page.text
        assert 'data-provider="codex"' not in page.text
        assert 'data-provider="gemini"' not in page.text
        assert "ANTHROPIC_BASE_URL" in page.text
        assert "已开通厂商：" in client.get("/user/account").text
        user_login(client, bob, bob_password)
        assert 'data-provider="claude"' not in client.get("/user/debug").text
        assert "尚未开通，请贡献 Worker" in client.get("/user/account").text


def test_debug_requires_login():
    with TestClient(app) as client:
        response = client.get("/user/debug", follow_redirects=False)
        assert response.status_code in {302, 303, 307}
        assert "/login" in response.headers["location"]
