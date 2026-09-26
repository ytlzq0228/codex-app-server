import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from codex_gateway.config import get_settings
from codex_gateway.main import app
from codex_gateway.admin_auth import SESSION_COOKIE

AJAX = {"X-Requested-With": "XMLHttpRequest"}


def csrf(client, path="/account"):
    page = client.get(path)
    assert page.status_code == 200, page.text
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def admin_login(client):
    settings = get_settings()
    assert client.post("/login", data={"username":settings.admin_username, "password":settings.admin_password.get_secret_value()}, follow_redirects=False).status_code == 302
    return csrf(client)


def new_user(client, token, role="user"):
    username = "test-" + uuid4().hex[:12]
    response = client.post("/admin/users", data={"csrf_token":token,"username":username,"role":role}, headers=AJAX)
    assert response.status_code == 200, response.text
    assert client.post("/admin/users/"+username+"/quota", data={"csrf_token":token,"amount":1}, headers=AJAX).status_code == 200
    return username, response.json()["secret"]


def user_login(client, username, password):
    response = client.post("/login", data={"username":username,"password":password}, follow_redirects=False)
    assert response.status_code == 302
    token = csrf(client)
    changed = client.post("/account/password", data={"csrf_token":token,"current_password":password,"new_password":"changed-"+password,"confirm_password":"changed-"+password}, headers=AJAX)
    assert changed.status_code == 200, changed.text
    return csrf(client)


def test_forced_password_and_role_boundaries():
    with TestClient(app) as client:
        token = admin_login(client)
        username,password = new_user(client,token)
        client.post("/login",data={"username":username,"password":password})
        user_csrf = csrf(client)
        assert client.post("/account/key",data={"csrf_token":user_csrf},headers=AJAX).status_code == 403
        token = user_login(client,username,password)
        assert client.get("/admin/users",headers=AJAX).status_code == 403
        assert client.get("/admin/finance",headers=AJAX).status_code == 403
        assert client.post("/account/key",data={"csrf_token":"wrong"},headers=AJAX).status_code == 403
        assert client.post("/admin/users",data={"csrf_token":token,"username":"hacker"},headers=AJAX).status_code == 403


def test_one_key_concurrency_rotation_and_request_details():
    with TestClient(app) as client:
        admin_token = admin_login(client)
        username,password = new_user(client,admin_token)
        token = user_login(client,username,password)
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: client.post("/account/key",data={"csrf_token":token},headers=AJAX),range(2)))
        assert sorted(r.status_code for r in responses) == [200,409]
        key = next(r.json() for r in responses if r.status_code == 200)
        before = key["secret"]
        rotated = client.post(f'/account/keys/{key["key_id"]}/rotate',data={"csrf_token":token},headers=AJAX).json()["secret"]
        assert before.split('_')[1] == rotated.split('_')[1]
        assert client.get('/v1/models',headers={'Authorization':'Bearer '+before}).status_code == 401
        response = client.post('/v1/responses',headers={'Authorization':'Bearer '+rotated},json={'model':'gpt-6-sol','input':'private request <script>alert(1)</script>'})
        assert response.status_code == 200, response.text
        detail = client.get('/usage/'+response.json()['id'])
        assert 'private request' in detail.text and '&lt;script&gt;' in detail.text
        other,other_password = None,None
        admin_token = admin_login(client)
        other,other_password = new_user(client,admin_token)
        user_login(client,other,other_password)
        assert client.get('/usage/'+response.json()['id']).status_code == 404
        assert client.post(f'/account/keys/{key["key_id"]}/rotate',data={"csrf_token":csrf(client)},headers=AJAX).status_code == 404


def test_session_restart_logout_revocation():
    with TestClient(app) as client:
        admin_login(client)
        cookie = client.cookies.get(SESSION_COOKIE)
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE,cookie)
        token = csrf(client)
        assert client.post('/logout',data={'csrf_token':token},follow_redirects=False).status_code == 302
        client.cookies.set(SESSION_COOKIE,cookie)
        assert client.get('/account',follow_redirects=False).status_code == 303


def test_finance_snapshot_and_transfer_keeps_history():
    with TestClient(app) as client:
        token = admin_login(client)
        first,pw = new_user(client,token)
        second,_ = new_user(client,token)
        assert client.post('/admin/prices',data={'csrf_token':token,'model':'gpt-6-sol','input_price':'2','output_price':'8'},headers=AJAX).status_code == 200
        user_token = user_login(client,first,pw)
        key = client.post('/account/key',data={'csrf_token':user_token},headers=AJAX).json()
        response = client.post('/v1/responses',headers={'Authorization':'Bearer '+key['secret']},json={'model':'gpt-6-sol','input':'hello world'}).json()
        request_id = response['id']
        assert '0.000028000000' in client.get('/usage/'+request_id).text
        token = admin_login(client)
        assert client.post('/admin/prices',data={'csrf_token':token,'model':'gpt-6-sol','input_price':'200','output_price':'800'},headers=AJAX).status_code == 200
        assert '0.000028000000' in client.get('/usage/'+request_id).text
        assert client.post('/admin/keys/'+key['key_id']+'/owner',data={'csrf_token':token,'username':second},headers=AJAX).status_code == 200
        assert client.get('/admin/finance').status_code == 200
        assert client.get('/admin/users').status_code == 200
        assert client.get('/debug').status_code == 200
        client.post('/login',data={'username':first,'password':'changed-'+pw})
        assert client.get('/usage/'+request_id).status_code == 200


def test_admin_cannot_promote_or_edit_superadmin():
    with TestClient(app) as client:
        token = admin_login(client)
        username,pw = new_user(client,token,'admin')
        token = user_login(client,username,pw)
        assert client.post('/admin/users',data={'csrf_token':token,'username':'escalate-'+uuid4().hex,'role':'superadmin'},headers=AJAX).status_code == 403
        assert client.post('/admin/users/'+get_settings().admin_username,data={'csrf_token':token,'role':'user'},headers=AJAX).status_code == 403


def test_google_state_verified_identity_and_replay(monkeypatch):
    subject = uuid4().hex
    email = subject+'@example.com'
    original_post,original_get = httpx.AsyncClient.post,httpx.AsyncClient.get
    async def post(client,url,**kwargs):
        if url == 'https://oauth2.googleapis.com/token':
            return httpx.Response(200,json={'access_token':'test-token'},request=httpx.Request('POST',url))
        return await original_post(client,url,**kwargs)
    async def get(client,url,**kwargs):
        if url == 'https://openidconnect.googleapis.com/v1/userinfo':
            return httpx.Response(200,json={'sub':subject,'email':email,'email_verified':True},request=httpx.Request('GET',url))
        return await original_get(client,url,**kwargs)
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    with TestClient(app) as client:
        token = admin_login(client)
        saved = client.post('/admin/google',data={'csrf_token':token,'enabled':'true','client_id':'test-client','client_secret':'test-secret','redirect_uri':'http://testserver/auth/google/callback'},headers=AJAX)
        assert saved.status_code == 200, saved.text
        assert 'test-secret' not in client.get('/admin/google').text
        response = client.get('/auth/google',follow_redirects=False)
        params = parse_qs(urlparse(response.headers['location']).query)
        assert params['code_challenge_method'] == ['S256']
        state = params['state'][0]
        assert client.get('/auth/google/callback?state=wrong&code=test').status_code == 400
        callback = '/auth/google/callback?state='+state+'&code=test'
        callback_response = client.get(callback,follow_redirects=False)
        assert callback_response.status_code == 302
        assert callback_response.headers['location'] == '/overview'
        assert client.get('/account').status_code == 200
        assert client.get('/admin/users',headers=AJAX).status_code == 403
        assert client.get(callback,follow_redirects=False).status_code == 400
        from codex_gateway.database import SessionLocal
        from codex_gateway.models import User
        async def verify_username():
            async with SessionLocal() as db:
                user=await db.get(User,email.split('@')[0])
                assert user and user.google_sub==subject and user.email==email
        client.portal.call(verify_username)
        subject=uuid4().hex
        response=client.get('/auth/google',follow_redirects=False)
        state=parse_qs(urlparse(response.headers['location']).query)['state'][0]
        assert client.get('/auth/google/callback?state='+state+'&code=test',follow_redirects=False).status_code==409


def test_rejected_requests_are_audited_with_original_params():
    with TestClient(app) as client:
        token = admin_login(client)
        username,pw = new_user(client,token)
        token = user_login(client,username,pw)
        key = client.post('/account/key',data={'csrf_token':token},headers=AJAX).json()['secret']
        response = client.post('/v1/responses',headers={'Authorization':'Bearer '+key},json={'model':'missing-model','input':'rejected request'})
        assert response.status_code == 400
        detail = client.get('/usage/'+response.headers['x-request-id'])
        assert detail.status_code == 200
        assert 'rejected request' in detail.text


def test_google_config_database_update_preserves_secret_and_permissions():
    with TestClient(app) as client:
        token = admin_login(client)
        data = {'csrf_token':token,'enabled':'true','client_id':'configured-client','client_secret':'private-test-value','redirect_uri':'https://gateway.example.com/auth/google/callback','trusted_domains':'example.com'}
        assert client.post('/admin/google',data=data,headers=AJAX).status_code == 200
        data.update(client_id='updated-client',client_secret='')
        assert client.post('/admin/google',data=data,headers=AJAX).status_code == 200
        page = client.get('/admin/google').text
        assert 'updated-client' in page and 'private-test-value' not in page and '已配置，留空保留' in page
        assert 'client_id=updated-client' in client.get('/auth/google',follow_redirects=False).headers['location']
        data['enabled'] = 'false'
        assert client.post('/admin/google',data=data,headers=AJAX).status_code == 200
        assert client.get('/auth/google',follow_redirects=False).status_code == 503
        user,password = new_user(client,token)
        user_login(client,user,password)
        assert client.get('/admin/google',headers=AJAX).status_code == 403


def test_disable_user_revokes_cookie_and_key():
    with TestClient(app) as client:
        token = admin_login(client)
        username,pw = new_user(client,token)
        user_token = user_login(client,username,pw)
        cookie = client.cookies.get(SESSION_COOKIE)
        key = client.post('/account/key',data={'csrf_token':user_token},headers=AJAX).json()['secret']
        token = admin_login(client)
        response = client.post('/admin/users/'+username,data={'csrf_token':token,'role':'user'},headers=AJAX)
        assert response.status_code == 200
        assert client.get('/v1/models',headers={'Authorization':'Bearer '+key}).status_code == 401
        assert client.get('/account',headers={'cookie':SESSION_COOKIE+'='+cookie},follow_redirects=False).status_code == 303
        assert client.post('/login',data={'username':username,'password':'changed-'+pw},follow_redirects=False).status_code == 401
