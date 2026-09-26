from fastapi.testclient import TestClient
from codex_gateway.main import app
from test_self_service import admin_login, user_login, AJAX
from test_quota_workers import create_person


def test_navigation_sections_and_canonical_routes():
    with TestClient(app) as client:
        admin_login(client)
        for path in ['/admin','/admin/api-keys','/admin/workers','/user/overview','/user/account','/user/workers','/user/usage','/user/debug']:
            response=client.get(path)
            assert response.status_code==200
            assert 'id="user-nav-heading"' in response.text
            assert 'id="admin-nav-heading"' in response.text
            assert 'href="/user/overview"' in response.text
            assert 'href="/admin/workers"' in response.text
            assert 'action="/auth/logout"' in response.text
        name,pw=create_person(client)
        user_login(client,name,pw)
        page=client.get('/user/overview').text
        assert 'id="user-nav-heading"' in page and 'id="admin-nav-heading"' not in page
        assert client.get('/admin',headers=AJAX).status_code==403
        for old in ['/account','/overview','/workers','/usage','/debug']:
            response=client.get(old,follow_redirects=False)
            assert response.status_code==307
            assert response.headers['location']=='/user'+old
        for old,new in [('/login','/auth/login'),('/user/login','/auth/login'),('/logout','/auth/logout'),('/user/logout','/auth/logout')]:
            response=client.get(old,follow_redirects=False)
            assert response.status_code==307
            assert response.headers['location']==new
        response=client.get('/user/auth/google/callback?state=example&code=example',follow_redirects=False)
        assert response.status_code==307
        assert response.headers['location']=='/auth/google/callback?state=example&code=example'
        assert client.post('/logout',follow_redirects=False).status_code==307
        manager,mpw=create_person(client,role='admin')
        user_login(client,manager,mpw)
        page=client.get('/user/overview').text
        assert 'id="user-nav-heading"' in page and 'id="admin-nav-heading"' in page
        client.cookies.clear()
        response=client.get('/user/account',follow_redirects=False)
        assert response.headers['location'].startswith('/auth/login?')
        assert 'action="/auth/login"' in client.get('/auth/login').text
