from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from codex_gateway.main import app
from codex_gateway.database import SessionLocal
from codex_gateway.models import ApiKey, User, Worker, WorkerStatus
from codex_gateway.config import get_settings
from codex_gateway.quota import quota_summary, reconcile_worker
from test_self_service import admin_login, csrf, user_login, AJAX


def create_person(client, role='user'):
    token=admin_login(client)
    name='quota-'+uuid4().hex[:12]
    response=client.post('/admin/users',data={'csrf_token':token,'username':name,'role':role},headers=AJAX)
    assert response.status_code==200,response.text
    return name,response.json()['secret']


def grant(client,name,amount):
    token=admin_login(client)
    response=client.post('/admin/users/'+name+'/quota',data={'csrf_token':token,'amount':amount},headers=AJAX)
    assert response.status_code==200,response.text


def signin(client,name,pw):
    response=client.post('/auth/login',data={'username':name,'password':'changed-'+pw},follow_redirects=False)
    assert response.status_code==302
    return csrf(client)


def new_key(client,token):
    return client.post('/user/account/key',data={'csrf_token':token},headers=AJAX)


async def summary(name):
    async with SessionLocal() as db:
        return await quota_summary(db,name)


@pytest.fixture
def worker_services(monkeypatch):
    import codex_gateway.admin as admin
    import codex_gateway.contributions as contributions
    original_post, original_delete = httpx.AsyncClient.post,httpx.AsyncClient.delete
    state={'account':{'type':'chatgpt','email':'contributor@example.com','planType':'plus'},'calls':0,'deleted':[]}
    async def post(client,url,**kwargs):
        if str(url)==get_settings().manager_url+'/workers':
            name=kwargs['json']['name']
            return httpx.Response(201,json={'name':name,'endpoint':'ws://'+name+':4500'})
        return await original_post(client,url,**kwargs)
    async def delete(client,url,**kwargs):
        if str(url).startswith(get_settings().manager_url+'/workers/'):
            state['deleted'].append(str(url))
            return httpx.Response(204)
        return await original_delete(client,url,**kwargs)
    class Server:
        async def call(self,method,params):
            state['calls']+=1
            if state.get('failure'):raise RuntimeError('Worker unavailable')
            if method=='account/read':return {'account':state['account']}
            return {'verificationUrl':'https://example.test/device','userCode':'TEST'}
    @asynccontextmanager
    async def opened(*args,**kwargs):yield Server()
    async def health(*args,**kwargs):pass
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    monkeypatch.setattr(httpx.AsyncClient,'delete',delete)
    monkeypatch.setattr(admin,'open_app_server',opened)
    monkeypatch.setattr(contributions,'open_app_server',opened)
    monkeypatch.setattr(admin,'run_healthcheck_turn',health)
    return state


def contribute(client,token):
    r=client.post('/user/workers',data={'csrf_token':token,'suffix':''},headers=AJAX)
    assert r.status_code==200,r.text
    return r.json()['worker_id']


def probe(client,token,worker):
    r=client.post('/user/workers/'+worker+'/probe',data={'csrf_token':token},headers=AJAX)
    assert r.status_code==200,r.text
    return r.json()


def test_default_zero_capacity_and_concurrent_enable():
    with TestClient(app) as client:
        name,pw=create_person(client)
        token=user_login(client,name,pw)
        assert client.portal.call(summary,name)['total']==0
        assert new_key(client,token).status_code==409
        grant(client,name,2)
        token=signin(client,name,pw)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results=list(pool.map(lambda _:new_key(client,token),range(3)))
        assert sorted(r.status_code for r in results)==[200,200,409]
        keys=[r.json()['key_id'] for r in results if r.status_code==200]
        assert client.portal.call(summary,name)['used']==2
        for key in keys:
            assert client.post('/user/account/keys/'+key+'/toggle',data={'csrf_token':token},headers=AJAX).status_code==200
        # A third, disabled key can exist; concurrent enable still cannot exceed two.
        extra=new_key(client,token).json()['key_id']
        assert client.post('/user/account/keys/'+extra+'/toggle',data={'csrf_token':token},headers=AJAX).status_code==200
        with ThreadPoolExecutor(max_workers=3) as pool:
            results=list(pool.map(lambda k:client.post('/user/account/keys/'+k+'/toggle',data={'csrf_token':token},headers=AJAX),keys+[extra]))
        assert sorted(r.status_code for r in results)==[200,200,409]
        assert client.portal.call(summary,name)['used']==2


def test_admin_sets_granted_quota_and_reduces_oldest_key():
    with TestClient(app) as client:
        name, password = create_person(client)
        grant(client, name, 2)
        token = user_login(client, name, password)
        older = new_key(client, token).json()
        newer = new_key(client, token).json()

        async def mark_newer_used():
            async with SessionLocal() as db:
                key = await db.get(ApiKey, UUID(newer['key_id']))
                key.last_used_at = datetime.now(timezone.utc)
                await db.commit()

        client.portal.call(mark_newer_used)
        admin_token = admin_login(client)
        page = client.get('/admin/users').text
        assert '编辑 Quota' in page and 'name="quota_granted"' in page
        response = client.post(f'/admin/users/{name}/quota', data={
            'csrf_token': admin_token, 'quota_granted': 1}, headers=AJAX)
        assert response.status_code == 200, response.text
        assert '已停用 1 个' in response.json()['message']
        assert client.portal.call(summary, name)['granted'] == 1
        assert client.get('/v1/models', headers={'Authorization': 'Bearer ' + older['secret']}).status_code == 401
        assert client.get('/v1/models', headers={'Authorization': 'Bearer ' + newer['secret']}).status_code == 200

        response = client.post(f'/admin/users/{name}/quota', data={
            'csrf_token': admin_token, 'quota_granted': 0}, headers=AJAX)
        assert response.status_code == 200
        assert client.portal.call(summary, name)['granted'] == 0
        assert client.portal.call(summary, name)['used'] == 0
        response = client.post(f'/admin/users/{name}/quota', data={
            'csrf_token': admin_token, 'quota_granted': 3}, headers=AJAX)
        assert response.status_code == 200
        assert client.portal.call(summary, name)['granted'] == 3


def test_worker_credit_lifecycle_and_lru(worker_services):
    with TestClient(app) as client:
        name,pw=create_person(client)
        grant(client,name,1)
        token=user_login(client,name,pw)
        worker=contribute(client,token)
        assert client.portal.call(summary,name)['contributed']==0
        worker_services['account']['planType']='free'
        probe(client,token,worker)
        assert client.portal.call(summary,name)['contributed']==0
        worker_services['account']['planType']='plus'
        probe(client,token,worker);probe(client,token,worker)
        assert client.portal.call(summary,name)['total']==2
        older=new_key(client,token).json()
        newer=new_key(client,token).json()
        async def mark_used():
            async with SessionLocal() as db:
                key=await db.get(ApiKey,UUID(newer['key_id']))
                key.last_used_at=datetime.now(timezone.utc)
                await db.commit()
        client.portal.call(mark_used)
        worker_services['account']=None
        probe(client,token,worker)
        assert client.portal.call(summary,name)['used']==1
        assert client.get('/v1/models',headers={'Authorization':'Bearer '+older['secret']}).status_code==401
        assert client.get('/v1/models',headers={'Authorization':'Bearer '+newer['secret']}).status_code==200
        worker_services['account']={'type':'chatgpt','email':'contributor@example.com','planType':'pro'}
        probe(client,token,worker)
        assert client.portal.call(summary,name)['available']==1
        assert client.post('/user/account/keys/'+older['key_id']+'/toggle',data={'csrf_token':token},headers=AJAX).status_code==200
        assert client.post('/user/workers/'+worker+'/delete',data={'csrf_token':token},headers=AJAX).status_code==200
        assert client.portal.call(summary,name)['contributed']==0
        assert client.portal.call(summary,name)['used']==1


def test_owner_isolation_account_read_and_admin_transfer(worker_services):
    with TestClient(app) as client:
        alice,pw=create_person(client)
        bob,bpw=create_person(client)
        token=user_login(client,alice,pw)
        worker=contribute(client,token)
        probe(client,token,worker)
        assert new_key(client,token).status_code == 200
        r=client.post('/user/workers/'+worker+'/account',data={'csrf_token':token},headers=AJAX)
        assert r.json()['account']['email']=='contributor@example.com'
        page=client.get('/user/workers')
        assert page.status_code==200 and 'contributor@example.com' in page.text and '+1 额度' in page.text
        token=user_login(client,bob,bpw)
        calls=worker_services['calls']
        for action in ['login','probe','account','delete']:
            assert client.post('/user/workers/'+worker+'/'+action,data={'csrf_token':token},headers=AJAX).status_code==404
        assert worker_services['calls']==calls and not worker_services['deleted']
        assert 'contributor@example.com' not in client.get('/user/workers').text
        assert client.post('/admin/workers/'+worker+'/owner',data={'csrf_token':token,'username':bob},headers=AJAX).status_code==403
        token=admin_login(client)
        assert client.post('/admin/workers/'+worker+'/owner',data={'csrf_token':token,'username':bob},headers=AJAX).status_code==200
        assert client.portal.call(summary,alice)['total']==0
        assert client.portal.call(summary,alice)['used']==0
        assert client.portal.call(summary,bob)['total']==1
        token=signin(client,alice,pw)
        assert client.post('/user/workers/'+worker+'/account',data={'csrf_token':token},headers=AJAX).status_code==404


def test_failed_account_read_removes_credit(worker_services):
    with TestClient(app) as client:
        name,pw=create_person(client)
        token=user_login(client,name,pw)
        worker=contribute(client,token);probe(client,token,worker)
        key=new_key(client,token).json()
        worker_services['failure']=True
        assert client.post('/user/workers/'+worker+'/account',data={'csrf_token':token},headers=AJAX).status_code==502
        assert client.portal.call(summary,name)['total']==0
        assert client.portal.call(summary,name)['used']==0
        assert client.get('/v1/models',headers={'Authorization':'Bearer '+key['secret']}).status_code==401


def test_admin_key_transfer_and_enable_respect_destination_capacity():
    with TestClient(app) as client:
        alice,pw=create_person(client)
        bob,bpw=create_person(client)
        grant(client,alice,1)
        token=user_login(client,alice,pw)
        key=new_key(client,token).json()['key_id']
        token=admin_login(client)
        assert client.post('/admin/keys/'+key+'/owner',data={'csrf_token':token,'username':bob},headers=AJAX).status_code==409
        assert client.post('/admin/keys/'+key+'/toggle',data={'csrf_token':token},headers=AJAX).status_code==200
        assert client.post('/admin/keys/'+key+'/owner',data={'csrf_token':token,'username':bob},headers=AJAX).status_code==200
        assert client.post('/admin/keys/'+key+'/toggle',data={'csrf_token':token},headers=AJAX).status_code==409
        grant(client,bob,1)
        token=csrf(client)
        assert client.post('/admin/keys/'+key+'/toggle',data={'csrf_token':token},headers=AJAX).status_code==200


def test_auto_monitor_detects_logout_and_disables_contributed_key(worker_services):
    from codex_gateway.contributions import refresh_worker_account
    with TestClient(app) as client:
        name,pw=create_person(client)
        token=user_login(client,name,pw)
        worker=contribute(client,token);probe(client,token,worker)
        assert new_key(client,token).status_code==200
        worker_services['account']=None
        client.portal.call(refresh_worker_account,UUID(worker))
        assert client.portal.call(summary,name)['used']==0
        assert client.portal.call(summary,name)['total']==0


def test_worker_names_and_pending_login_guard(worker_services):
    with TestClient(app) as client:
        name,pw=create_person(client)
        token=user_login(client,name,pw)
        for suffix in ['00','100','-1','1.5','ab','１２',' 1']:
            r=client.post('/user/workers',data={'csrf_token':token,'suffix':suffix},headers=AJAX)
            assert r.status_code==400,r.text
        assert client.post('/user/workers',data={'csrf_token':token,'name':'someone-worker-01'},headers=AJAX).status_code==400
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:client.post('/user/workers',data={'csrf_token':token},headers=AJAX),range(2)))
        assert sorted(r.status_code for r in results)==[200,409]
        worker=next(r.json()['worker_id'] for r in results if r.status_code==200)
        page=client.get('/user/workers').text
        assert name+'-worker-01' in page
        assert 'data-modal-open="contribute-worker" disabled' in page
        worker_services['account']['planType']='free'
        probe(client,token,worker)
        assert 'value="02"' in client.get('/user/workers').text
        r=client.post('/user/workers',data={'csrf_token':token,'suffix':'7'},headers=AJAX)
        assert r.status_code==200,r.text
        second=r.json()['worker_id']
        assert name+'-worker-07' in client.get('/user/workers').text
        assert client.post('/user/workers/'+second+'/delete',data={'csrf_token':token},headers=AJAX).status_code==200
        assert 'value="08"' in client.get('/user/workers').text
        assert client.post('/user/workers',data={'csrf_token':token,'suffix':'07'},headers=AJAX).status_code==409
        third=contribute(client,token)
        probe(client,token,third)
        worker_services['account']=None
        probe(client,token,worker)
        assert client.post('/user/workers',data={'csrf_token':token},headers=AJAX).status_code==409


def test_admin_worker_page_is_separate_from_contribution_page(worker_services):
    with TestClient(app) as client:
        token=admin_login(client)
        assert client.post('/user/workers',data={'csrf_token':token},headers=AJAX).status_code==409
        page=client.get('/user/workers').text
        assert '我的 Worker' in page and '更改归属' not in page
        names=[f"admin-system-{uuid4().hex[:8]}", f"admin-system-{uuid4().hex[:8]}"]
        for name in names:
            r=client.post('/admin/workers',data={'csrf_token':token,'name':name},headers=AJAX)
            assert r.status_code==200,r.text
        admin_page=client.get('/admin/workers').text
        assert 'Worker 管理' in admin_page and all(name in admin_page for name in names)
        assert 'href="/admin/workers"' in client.get('/admin').text


def test_duplicate_account_quota_lifecycle_and_transfer(worker_services):
    with TestClient(app) as client:
        alice,pw=create_person(client)
        bob,bpw=create_person(client)
        token=user_login(client,alice,pw)
        first=contribute(client,token);probe(client,token,first)
        second=contribute(client,token)
        worker_services['account']['email']='  CONTRIBUTOR@example.com  '
        probe(client,token,second)
        assert client.portal.call(summary,alice)['contributed']==1
        assert '重复账号，不增加额度' in client.get('/user/workers').text
        older=new_key(client,token).json()['key_id']
        assert new_key(client,token).status_code==409
        worker_services['account']['email']='another@example.com'
        probe(client,token,second)
        assert client.portal.call(summary,alice)['contributed']==2
        newer=new_key(client,token).json()['key_id']
        async def mark_used():
            async with SessionLocal() as db:
                key=await db.get(ApiKey,UUID(newer));key.last_used_at=datetime.now(timezone.utc)
                await db.commit()
        client.portal.call(mark_used)
        worker_services['account']['email']='contributor@example.com'
        probe(client,token,second)
        assert client.portal.call(summary,alice)['used']==1
        async def check_keys():
            async with SessionLocal() as db:
                assert not (await db.get(ApiKey,UUID(older))).enabled
                assert (await db.get(ApiKey,UUID(newer))).enabled
        client.portal.call(check_keys)
        worker_services['account']=None
        probe(client,token,first)
        assert client.portal.call(summary,alice)['contributed']==1
        worker_services['account']={'type':'chatgpt','email':'contributor@example.com','planType':'plus'}
        probe(client,token,first)
        token=admin_login(client)
        assert client.post('/admin/workers/'+second+'/owner',data={'csrf_token':token,'username':bob},headers=AJAX).status_code==200
        assert client.portal.call(summary,alice)['contributed']==1
        assert client.portal.call(summary,bob)['contributed']==1
        assert client.post('/admin/workers/'+second+'/owner',data={'csrf_token':token,'username':alice},headers=AJAX).status_code==200
        assert client.portal.call(summary,bob)['contributed']==0
        assert client.portal.call(summary,alice)['contributed']==1
        token=signin(client,alice,pw)
        assert client.post('/user/workers/'+first+'/delete',data={'csrf_token':token},headers=AJAX).status_code==200
        assert client.portal.call(summary,alice)['contributed']==1
        worker_services['account']['email']=None
        probe(client,token,second)
        assert client.portal.call(summary,alice)['contributed']==0
        assert client.portal.call(summary,alice)['used']==0


@pytest.mark.parametrize('failure', [None, 'account/logout', 'account/login/start'])
def test_relogin_logout_order_and_failure_state(worker_services, monkeypatch, failure):
    import codex_gateway.admin as admin
    from codex_gateway.app_server import AppServerError
    calls=[]
    class Server:
        async def call(self,method,params):
            calls.append(method)
            if method==failure:raise AppServerError('test failure')
            if method=='account/read':return {'account':worker_services['account']}
            return {'verificationUrl':'https://example.test/device','userCode':'TEST'}
    @asynccontextmanager
    async def opened(*args,**kwargs):yield Server()
    with TestClient(app) as client:
        name,pw=create_person(client)
        token=user_login(client,name,pw)
        worker=contribute(client,token);probe(client,token,worker)
        assert new_key(client,token).status_code==200
        assert '是否退出当前账号并重新登录' in client.get('/user/workers').text
        monkeypatch.setattr(admin,'open_app_server',opened)
        response=client.post('/user/workers/'+worker+'/login',data={'csrf_token':token,'force':'true'},headers=AJAX)
        assert response.status_code==(502 if failure else 200)
        expected=['account/read','account/logout']
        if failure!='account/logout':expected+=['account/login/start']
        assert calls==expected
        quota=client.portal.call(summary,name)
        assert quota['contributed']==(1 if failure=='account/logout' else 0)
        assert quota['used']==(1 if failure=='account/logout' else 0)


def test_any_identified_non_free_plan_contributes(worker_services):
    with TestClient(app) as client:
        name,pw=create_person(client)
        token=user_login(client,name,pw)
        worker=contribute(client,token)
        for plan,expected in [('self_serve_business_prolite',1),('future_subscription',1),
                              (' Pro ',1),('free',0),(' FREE ',0),(None,0),('',0),('  ',0)]:
            worker_services['account']['planType']=plan
            probe(client,token,worker)
            assert client.portal.call(summary,name)['contributed']==expected,plan
        worker_services['account']['planType']='self_serve_business_prolite'
        probe(client,token,worker)
        second=contribute(client,token);probe(client,token,second)
        assert client.portal.call(summary,name)['contributed']==1
        assert '重复账号，不增加额度' in client.get('/user/workers').text
