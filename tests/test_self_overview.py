from uuid import uuid4, UUID
from fastapi.testclient import TestClient
from codex_gateway.main import app
from codex_gateway.database import SessionLocal
from codex_gateway.models import Worker, WorkerStatus
from test_self_service import admin_login, user_login, AJAX
from test_quota_workers import create_person, contribute, probe, new_key, signin, worker_services


def test_overview_lifecycle_and_isolation(worker_services):
    with TestClient(app) as client:
        alice,pw=create_person(client)
        bob,bpw=create_person(client)
        token=user_login(client,alice,pw)
        response=client.post('/auth/login',data={'username':alice,'password':'changed-'+pw},follow_redirects=False)
        assert response.headers['location']=='/user/overview'
        token=signin(client,alice,pw)
        page=client.get('/user/overview')
        assert page.status_code==200
        assert '尚未创建 Worker' in page.text and '尚未生成 Key' in page.text
        assert '0 / 3 项已就绪' in page.text
        assert '非常好，一切正常' not in page.text
        worker=contribute(client,token)
        assert '已创建，等待登录' in client.get('/user/overview').text
        probe(client,token,worker)
        assert new_key(client,token).status_code==200
        page=client.get('/user/overview').text
        assert '3 / 3 项已就绪' in page and '非常好，一切正常' in page
        assert '已生成 1 个 Key' in page
        async def limit():
            async with SessionLocal() as db:
                w=await db.get(Worker,UUID(worker))
                w.status=WorkerStatus.error
                w.failure_kind='limit'
                await db.commit()
        client.portal.call(limit)
        page=client.get('/user/overview').text
        assert '账号已超限额' in page and '非常好，一切正常' not in page
        token=user_login(client,bob,bpw)
        page=client.get('/user/overview').text
        assert alice+'-worker-01' not in page
        assert '尚未创建 Worker' in page and '尚未生成 Key' in page
        assert 'href="/user/overview"' in client.get('/user/account').text
        admin_login(client)
        assert client.get('/user/overview',follow_redirects=False).status_code==200


def test_initial_password_keeps_required_password_change():
    with TestClient(app) as client:
        name,pw=create_person(client)
        response=client.post('/auth/login',data={'username':name,'password':pw},follow_redirects=False)
        assert response.headers['location']=='/user/account'
        assert client.get('/user/overview',follow_redirects=False).headers['location']=='/user/account'
