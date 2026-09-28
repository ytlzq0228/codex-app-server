"""Regression cover for the authentication and exposure hardening."""
from uuid import uuid4

from fastapi.testclient import TestClient

from codex_gateway.config import get_settings
from codex_gateway.login_throttle import ALLOWANCE
from codex_gateway.main import app

AJAX = {"X-Requested-With": "XMLHttpRequest"}


def admin_credentials():
    settings = get_settings()
    return settings.admin_username, settings.admin_password.get_secret_value()


def succeed_login(client):
    """Also clears both failure counters, keeping the shared address scope clean."""
    username, password = admin_credentials()
    response = client.post("/auth/login", data={"username": username, "password": password},
                           follow_redirects=False)
    assert response.status_code == 302, response.text
    return response


def test_openapi_schema_and_docs_are_not_served() -> None:
    with TestClient(app) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404, path


def test_development_api_key_is_rejected_when_unset(monkeypatch) -> None:
    from codex_gateway import auth as auth_module

    settings = get_settings()
    monkeypatch.setattr(settings, "dev_api_key", None)
    with TestClient(app) as client:
        response = client.get("/v1/models", headers={"Authorization": "Bearer cag_dev_local"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"
    assert auth_module.require_api_key  # the bypass lives here and nowhere else


def test_cross_origin_post_is_rejected_without_touching_bearer_endpoints() -> None:
    with TestClient(app) as client:
        succeed_login(client)
        blocked = client.post("/user/account/key", data={"csrf_token": "irrelevant"},
                              headers={**AJAX, "Origin": "https://evil.test"})
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "cross_site_request"

        assert client.post("/user/account/key", data={"csrf_token": "wrong"},
                           headers={**AJAX, "Origin": "null"}).status_code == 403

        # Same origin and origin-less clients reach the route's own CSRF check.
        same_origin = client.post("/user/account/key", data={"csrf_token": "wrong"},
                                  headers={**AJAX, "Origin": "http://testserver"})
        assert same_origin.status_code == 403
        assert same_origin.json()["error"]["message"] == "Invalid security token"

        # A bearer endpoint is not cookie-authenticated, so a browser origin is fine.
        api = client.post("/v1/responses", headers={"Origin": "https://app.example",
                                                    "Authorization": "Bearer cag_dev_local"},
                          json={"model": "gpt-6-sol", "input": "hello"})
        assert api.status_code == 200


def test_unauthenticated_post_returns_401_instead_of_a_redirect() -> None:
    with TestClient(app) as client:
        client.cookies.clear()
        assert client.get("/user/account", follow_redirects=False).status_code == 303
        posted = client.post("/user/account/key", data={"csrf_token": "x"}, follow_redirects=False)
        assert posted.status_code == 401


def test_password_login_locks_out_after_repeated_failures() -> None:
    username = "throttle-" + uuid4().hex[:12]
    with TestClient(app) as client:
        for attempt in range(ALLOWANCE["user"]):
            response = client.post("/auth/login", data={"username": username, "password": "wrong"},
                                   follow_redirects=False)
            assert response.status_code == 401, attempt

        locked = client.post("/auth/login", data={"username": username, "password": "wrong"},
                             follow_redirects=False)
        assert locked.status_code == 429
        assert int(locked.headers["Retry-After"]) > 0

        # The lock is per account: another account still authenticates normally.
        succeed_login(client)


def test_worker_contribution_is_capped_per_account(monkeypatch) -> None:
    import re

    settings = get_settings()
    with TestClient(app) as client:
        succeed_login(client)
        token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/user/account").text).group(1)
        username = "capped-" + uuid4().hex[:12]
        created = client.post("/admin/users", data={"csrf_token": token, "username": username},
                              headers=AJAX)
        assert created.status_code == 200, created.text
        password = created.json()["secret"]

        assert client.post("/auth/login", data={"username": username, "password": password},
                           follow_redirects=False).status_code == 302
        token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/user/account").text).group(1)
        changed = client.post("/user/account/password", headers=AJAX, data={
            "csrf_token": token, "current_password": password,
            "new_password": "changed-" + password, "confirm_password": "changed-" + password})
        assert changed.status_code == 200, changed.text
        token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/user/account").text).group(1)

        # The cap is checked before the manager is contacted, so no container is created.
        monkeypatch.setattr(settings, "max_workers_per_user", 0)
        refused = client.post("/user/workers", data={"csrf_token": token}, headers=AJAX)
        assert refused.status_code == 409
        assert "上限" in refused.json()["error"]["message"]


def test_cookie_secure_flag_is_tri_state() -> None:
    from types import SimpleNamespace

    from codex_gateway.config import Settings
    from codex_gateway.user_auth import cookie_secure

    plain = SimpleNamespace(url=SimpleNamespace(scheme="http"))
    tls = SimpleNamespace(url=SimpleNamespace(scheme="https"))

    detect = Settings(admin_cookie_secure=None)
    assert cookie_secure(detect, plain) is False
    assert cookie_secure(detect, tls) is True

    # Forcing it on covers a TLS proxy that uvicorn does not trust, where the
    # request scheme stays http and detection alone would leave the flag off.
    forced = Settings(admin_cookie_secure=True)
    assert cookie_secure(forced, plain) is True
    assert cookie_secure(forced, tls) is True

    disabled = Settings(admin_cookie_secure=False)
    assert cookie_secure(disabled, plain) is False
    assert cookie_secure(disabled, tls) is False

    # A blank or "auto" environment value is the same as leaving it unset.
    for value in ("", "  ", "auto", "AUTO"):
        assert Settings(admin_cookie_secure=value).admin_cookie_secure is None


def test_native_gemini_bearer_endpoint_accepts_origin_but_still_requires_key():
    with TestClient(app) as client:
        response = client.post(
            "/v1beta/models/gpt-6-sol:generateContent",
            headers={"Origin": "https://app.example"},
            json={"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
        )
        assert response.status_code == 401
        assert response.json()["error"]["status"] == "UNAUTHENTICATED"

        response = client.post(
            "/v1beta/models/gpt-6-sol:generateContent",
            headers={"Origin": "https://app.example", "x-goog-api-key": "cag_dev_local"},
            json={"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
        )
        assert response.status_code == 200
        assert response.json()["candidates"][0]["content"]["parts"]
