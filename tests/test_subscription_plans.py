from decimal import Decimal
from uuid import uuid4, UUID

from fastapi.testclient import TestClient
from sqlalchemy import select, delete

from codex_gateway.main import app
from codex_gateway.database import SessionLocal
from codex_gateway.models import Worker, WorkerStatus, SubscriptionPlan
from codex_gateway.subscriptions import subscription_summary
from codex_gateway.backend import WorkerFailure
from test_self_service import admin_login, AJAX, user_login
from test_quota_workers import create_person, contribute, probe, worker_services


def test_plan_prices_discovery_totals_and_permissions():
    plan = 'plan-' + uuid4().hex[:12]
    custom = 'custom-' + uuid4().hex[:12]
    ids = []
    async def seed():
        async with SessionLocal() as db:
            for mode,kind,enabled,removed in [('chatgpt',None,True,False),('chatgpt','limit',True,False),
                    ('chatgpt',None,False,False),('chatgpt','logged_out',True,False),
                    (None,None,True,False),('chatgpt',None,True,True)]:
                ident=uuid4()
                ids.append(ident)
                db.add(Worker(id=ident,name='subscription-'+ident.hex,container_name=ident.hex,
                    endpoint='removed://worker' if removed else 'ws://test',plan_type=plan,
                    auth_mode=mode,failure_kind=kind,enabled=enabled,status=WorkerStatus.error))
            await db.commit()
    async def row():
        async with SessionLocal() as db:
            summary = await subscription_summary(db)
            await db.commit()
            return next(r for r in summary['rows'] if r['name']==plan)
    async def cleanup():
        async with SessionLocal() as db:
            await db.execute(delete(Worker).where(Worker.id.in_(ids)))
            await db.commit()
    with TestClient(app) as client:
        token=admin_login(client)
        client.portal.call(seed)
        try:
            page=client.get('/admin/finance')
            assert page.status_code==200 and plan in page.text and '新增套餐' in page.text
            result=client.portal.call(row)
            assert result['count']==3 and result['price'] is None
            for amount in ['-1','1000000000','NaN','Infinity']:
                r=client.post('/admin/subscription-plans',data={'csrf_token':token,'name':plan,'monthly_price':amount},headers=AJAX)
                assert r.status_code in (400,422)
            for name in ['   ', 'x'*121]:
                assert client.post('/admin/subscription-plans',data={'csrf_token':token,'name':name,'monthly_price':'20'},headers=AJAX).status_code in (400,422)
            assert client.post('/admin/subscription-plans',data={'csrf_token':'bad','name':plan,'monthly_price':'20'},headers=AJAX).status_code==403
            for amount in ['20','25.123456','0']:
                r=client.post('/admin/subscription-plans',data={'csrf_token':token,'name':' '+plan.upper()+' ','monthly_price':amount},headers=AJAX)
                assert r.status_code==200,r.text
                result=client.portal.call(row)
                assert result['price']==Decimal(amount) and result['subtotal']==3*Decimal(amount)
            assert client.post('/admin/subscription-plans',data={'csrf_token':token,'name':custom,'monthly_price':'12'},headers=AJAX).status_code==200
            for url in ['/admin/finance','/admin/reports?month=all']:
                assert custom in client.get(url).text
            name,pw=create_person(client)
            user_token=user_login(client,name,pw)
            assert client.post('/admin/subscription-plans',data={'csrf_token':user_token,'name':plan,'monthly_price':'1'},headers=AJAX).status_code==403
        finally:
            client.portal.call(cleanup)


def test_limit_preserves_subscription_and_logout_retains_plan(worker_services, monkeypatch):
    import codex_gateway.admin as admin
    plan='detected-'+uuid4().hex[:12]
    worker_services['account']['planType']=plan
    async def limited(*args,**kwargs):
        raise WorkerFailure('Usage limit',kind='limit',safe_to_retry=True)
    async def snapshot(worker_id):
        async with SessionLocal() as db:
            worker=await db.get(Worker,UUID(worker_id))
            summary=await subscription_summary(db)
            await db.commit()
            row=next(r for r in summary['rows'] if r['name']==plan)
            return worker.auth_mode, worker.failure_kind, row
    with TestClient(app) as client:
        name,pw=create_person(client)
        token=user_login(client,name,pw)
        worker=contribute(client,token)
        monkeypatch.setattr(admin,'run_healthcheck_turn',limited)
        result=probe(client,token,worker)
        assert not result['ok'] and result['logged_in']
        mode,kind,row=client.portal.call(snapshot,worker)
        assert mode=='chatgpt' and kind=='limit' and row['count']==1
        worker_services['account']=None
        probe(client,token,worker)
        mode,kind,row=client.portal.call(snapshot,worker)
        assert mode is None and kind=='logged_out' and row['count']==0
        token=admin_login(client)
        assert plan in client.get('/admin/finance').text
