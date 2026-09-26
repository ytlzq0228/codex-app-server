"""Error negotiation tests do not connect to or modify any database."""
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from codex_gateway.admin_auth import require_admin
from codex_gateway.auth import require_api_key
from codex_gateway.database import get_session
from codex_gateway.main import app


def test_browser_forbidden_page_and_json_contracts():
    async def forbidden():
        raise HTTPException(403, '需要管理员权限')

    async def no_database():
        yield None

    previous = app.dependency_overrides.copy()
    app.dependency_overrides.update({require_admin: forbidden, require_api_key: forbidden, get_session: no_database})
    try:
        # No lifespan: avoid all startup migrations and database writes.
        client = TestClient(app)
        response = client.get('/admin/users', headers={'Accept': 'text/html'})
        assert response.status_code == 403
        assert response.headers['content-type'] == 'text/html; charset=utf-8'
        assert response.headers['cache-control'] == 'no-store'
        assert '无权访问此页面' in response.text
        assert 'href="/user/account"' in response.text
        assert 'href="/auth/login"' in response.text
        for path, headers in [
            ('/admin/users', {'Accept': 'application/json'}),
            ('/admin/users', {'Accept': 'text/html', 'X-Requested-With': 'XMLHttpRequest'}),
            ('/v1/models', {'Accept': 'text/html'}),
        ]:
            response = client.get(path, headers=headers)
            assert response.status_code == 403
            assert response.json()['error']['message'] == '需要管理员权限'
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
