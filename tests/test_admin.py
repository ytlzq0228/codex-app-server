import re
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import codex_gateway.admin as admin_module
from codex_gateway.admin import login_worker_endpoint, manager_delete_succeeded
from codex_gateway.admin_auth import SESSION_COOKIE
from codex_gateway.config import get_settings
from codex_gateway.main import app


def login(client: TestClient) -> str:
    settings = get_settings()
    response = client.post(
        "/auth/login",
        data={"username": settings.admin_username, "password": settings.admin_password.get_secret_value(), "next": "/admin"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "codex_admin_session=" in response.headers["set-cookie"]
    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text)
    assert match
    return match.group(1)


def test_admin_redirects_to_login_without_cookie() -> None:
    with TestClient(app) as client:
        response = client.get("/admin", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/auth/login?next=/admin")
        assert 'name="password"' in client.get("/auth/login").text
        assert "[hidden]{display:none!important}" in client.get("/static/admin.css").text


def test_admin_cookie_login_dashboard_and_logout() -> None:
    with TestClient(app) as client:
        csrf = login(client)
        dashboard = client.get("/admin")
        assert "运行控制台" in dashboard.text
        assert "API Keys" in dashboard.text
        assert "Key 活动会话" in dashboard.text
        assert "请求历史" in dashboard.text
        assert "/static/admin-features.css" in dashboard.text
        assert 'href="/user/account"' in dashboard.text
        assert 'id="password-dialog"' not in dashboard.text
        assert "HTTPBasic" not in dashboard.text

        response = client.post("/auth/logout", data={"csrf_token": csrf}, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/auth/login"


def test_admin_navigation_uses_four_isolated_pages() -> None:
    with TestClient(app) as client:
        login(client)
        overview = client.get("/admin").text
        keys = client.get("/admin/api-keys").text
        workers = client.get("/admin/workers").text
        sessions = client.get("/admin/sessions").text
        history = client.get("/admin/history").text

        assert 'id="overview"' in overview and 'id="monitoring"' in overview
        assert 'id="workers"' not in overview and 'id="keys"' not in overview
        assert 'id="sessions"' not in overview and 'id="history"' not in overview
        assert 'id="keys"' in keys and 'id="workers"' not in keys and 'id="overview"' not in keys
        assert 'id="workers"' in workers and 'id="keys"' not in workers and 'Worker 管理' in workers
        assert 'data-edit-worker-owner' in workers
        assert 'id="worker-owner-dialog"' in workers
        assert 'data-edit-worker-name' in workers
        assert 'id="worker-name-dialog"' in workers
        assert 'name="name" required maxlength="80"' in workers
        assert 'name="username"' in workers and '保存归属' in workers
        assert '<th>归属</th><th>登录账号 / 套餐</th><th>认证方式</th>' in workers
        assert '归属 / 登录账号' not in workers
        assert 'id="sessions"' in sessions and 'id="keys"' not in sessions
        assert 'id="history"' in history and 'id="sessions"' not in history
        assert 'class="active" href="/admin/history"' in history
        assert 'class="active" href="/admin/workers"' in workers


def test_admin_can_change_password_and_invalidate_old_session() -> None:
    settings = get_settings()
    old_password = settings.admin_password.get_secret_value()
    new_password = f"changed-{uuid4().hex}"
    with TestClient(app) as client:
        csrf = login(client)
        old_cookie = client.cookies.get(SESSION_COOKIE)

        wrong = client.post(
            "/user/account/password",
            data={"current_password": "wrong", "new_password": new_password, "confirm_password": new_password, "csrf_token": csrf},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert wrong.status_code == 400

        changed = client.post(
            "/user/account/password",
            data={"current_password": old_password, "new_password": new_password, "confirm_password": new_password, "csrf_token": csrf},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert changed.status_code == 200
        assert SESSION_COOKIE in changed.headers["set-cookie"]
        assert client.get("/admin", headers={"cookie": f"{SESSION_COOKIE}={old_cookie}"}, follow_redirects=False).status_code == 303
        assert client.post("/auth/login", data={"username": settings.admin_username, "password": old_password, "next": "/admin"}, follow_redirects=False).status_code == 401
        assert client.post("/auth/login", data={"username": settings.admin_username, "password": new_password, "next": "/admin"}, follow_redirects=False).status_code == 302

        dashboard = client.get("/admin")
        new_csrf = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text).group(1)
        restored = client.post(
            "/user/account/password",
            data={"current_password": new_password, "new_password": old_password, "confirm_password": old_password, "csrf_token": new_csrf},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert restored.status_code == 200


def test_key_can_be_edited_and_soft_deleted_without_losing_history() -> None:
    with TestClient(app) as client:
        csrf = login(client)
        assert client.post("/admin/users/"+get_settings().admin_username+"/quota", data={"csrf_token":csrf,"amount":1}, headers={"X-Requested-With":"XMLHttpRequest"}).status_code == 200
        original_name = f"admin-lifecycle-{uuid4().hex[:8]}"
        renamed = f"admin-renamed-{uuid4().hex[:8]}"
        created = client.post(
            "/admin/keys",
            data={"name": original_name, "scheduling_mode": "pooled", "pinned_worker_id": "", "csrf_token": csrf},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert created.status_code == 200
        payload = created.json()
        key_id = payload["key_id"]
        raw_key = payload["secret"]

        edited = client.post(
            f"/admin/keys/{key_id}/edit",
            data={"name": renamed, "scheduling_mode": "pooled", "pinned_worker_id": "", "csrf_token": csrf},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert edited.status_code == 200
        assert renamed in client.get("/admin/api-keys").text

        assert client.get("/v1/models", headers={"Authorization": f"Bearer {raw_key}"}).status_code == 200
        api_response = client.post("/v1/responses", headers={"Authorization": f"Bearer {raw_key}"}, json={"model": "gpt-6-sol", "input": "history retention test"})
        assert api_response.status_code == 200
        request_id = api_response.json()["id"]
        released = client.post(
            f"/admin/sessions/{request_id}/delete",
            data={"csrf_token": csrf},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert released.status_code == 200
        second_response = client.post("/v1/responses", headers={"Authorization": f"Bearer {raw_key}"}, json={"model": "gpt-6-sol", "input": "second session"})
        assert second_response.status_code == 200
        cleared = client.post(
            f"/admin/keys/{key_id}/sessions/clear",
            data={"csrf_token": csrf},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert cleared.status_code == 200
        deleted = client.post(
            f"/admin/keys/{key_id}/delete",
            data={"csrf_token": csrf},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert deleted.status_code == 200
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {raw_key}"}).status_code == 401
        history_page = client.get("/admin/history").text
        keys_page = client.get("/admin/api-keys").text
        assert request_id in history_page
        assert renamed in history_page
        assert f"/admin/keys/{key_id}/edit" not in keys_page


def test_admin_ajax_action_requires_csrf() -> None:
    with TestClient(app) as client:
        login(client)
        response = client.post(
            "/admin/keys",
            data={"name": "test", "scheduling_mode": "pooled", "csrf_token": "wrong"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert response.status_code == 403


def test_forged_session_cookie_is_rejected() -> None:
    with TestClient(app) as client:
        csrf = login(client)
        cookie = client.cookies.get(SESSION_COOKIE)
        for forged in ("garbage", cookie + "x", cookie[:-1]):
            response = client.get("/admin", headers={"cookie": f"{SESSION_COOKIE}={forged}"}, follow_redirects=False)
            assert response.status_code == 303, forged
        assert csrf


def test_delete_worker_treats_missing_container_as_success() -> None:
    assert manager_delete_succeeded(204)
    assert manager_delete_succeeded(404)
    assert not manager_delete_succeeded(403)
    assert not manager_delete_succeeded(409)


@pytest.mark.asyncio
async def test_login_does_not_start_device_flow_when_already_logged_in(monkeypatch) -> None:
    calls: list[str] = []

    class FakeAppServer:
        async def call(self, method: str, params: dict):
            calls.append(method)
            return {"account": {"type": "chatgpt", "planType": "pro"}}

    @asynccontextmanager
    async def fake_open(*_args, **_kwargs):
        yield FakeAppServer()

    monkeypatch.setattr(admin_module, "open_app_server", fake_open)
    result = await login_worker_endpoint("ws://worker:4500", get_settings())
    assert result["logged_in"] is True
    assert calls == ["account/read"]


@pytest.mark.asyncio
async def test_login_starts_device_flow_for_logged_out_worker(monkeypatch) -> None:
    class FakeAppServer:
        async def call(self, method: str, params: dict):
            if method == "account/read":
                return {"account": None}
            return {"verificationUrl": "https://example.test/device", "userCode": "ABCD-EFGH"}

    @asynccontextmanager
    async def fake_open(*_args, **_kwargs):
        yield FakeAppServer()

    monkeypatch.setattr(admin_module, "open_app_server", fake_open)
    result = await login_worker_endpoint("ws://worker:4500", get_settings(), poll_url="/probe")
    assert result["logged_in"] is False
    assert result["user_code"] == "ABCD-EFGH"
    assert result["poll_url"] == "/probe"
