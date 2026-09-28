from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
import re
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from codex_gateway import execution as ex
from codex_gateway import main
from codex_gateway.auth import ApiPrincipal
from codex_gateway.backend import BackendTarget, WorkerFailure
from codex_gateway.binding_lifecycle import invalidate_bindings
from codex_gateway.config import get_settings
from codex_gateway.contributions import update_account
from codex_gateway.database import SessionLocal
from codex_gateway.models import ApiKey, Worker, ExecutionSession, ResponseBinding, UsageRecord
from codex_gateway.schemas import BackendResult, BackendStreamEvent
from test_execution import req, audit_for


async def seed():
    async with SessionLocal() as db:
        key=ApiKey(name='lifecycle-'+uuid4().hex,prefix=uuid4().hex[:20],key_hash=uuid4().hex*2)
        w=Worker(name='life-'+uuid4().hex,container_name=uuid4().hex,endpoint='ws://test',status='ready',auth_mode='chatgpt',account_email='old@example.test')
        db.add_all([key,w]);await db.commit()
    return key,w,ApiPrincipal(key.id,'test'),BackendTarget(str(key.id)+':'+str(w.id),w.endpoint,'/tmp',w.id,w.execution_generation)


async def checkpoint(key,w,p,t,*,tool=False):
    r=req();client_id=str(uuid4());a=audit_for(r,client_id)
    await ex.prepare(r,p,'responses',a)
    rid='resp_'+uuid4().hex;tid='t-'+uuid4().hex
    call={'type':'function_call','call_id':'call_'+uuid4().hex,'name':'lookup','arguments':'{}'}
    result=BackendResult(text='' if tool else 'answer',thread_id=tid,tool_calls=[call] if tool else [])
    async with SessionLocal() as db:
        db.add(ResponseBinding(response_id=rid,api_key_id=key.id,worker_id=w.id,thread_id=tid,last_used_at=ex.now()-timedelta(days=3),expires_at=ex.now()-timedelta(days=2)))
        await ex.finish(db,a,result,t,rid);await db.commit()
    await ex.cleanup(a)
    full=[*r.input,call,{'type':'function_call_output','call_id':call['call_id'],'output':'ok'}] if tool else [*r.input,{'role':'assistant','content':'answer'}]
    follow=req([*full,{'role':'user','content':'next'}])
    return a,audit_for(follow,client_id),follow,rid,tid


def test_idle_binding_resumes_and_events_fence_stale_writers():
    async def run():
        key,w,p,t=await seed();a,b,follow,rid,tid=await checkpoint(key,w,p,t)
        prepared,binding=await ex.prepare(follow,p,'responses',b)
        assert binding.response_id==rid and prepared.input_text()=='USER:\nnext'
        async with SessionLocal() as db:
            assert await invalidate_bindings(db,api_key_id=key.id,thread_id=tid,reason='administrator_released')==1
            await db.commit()
        async with SessionLocal() as db:
            assert not await ex.finish(db,b,BackendResult(text='late',thread_id=tid),t,'late')
            assert (await db.get(ResponseBinding,rid)).status=='invalid'
        await ex.cleanup(b)
        from codex_gateway.client_tools import ToolProtocolError
        async def cancelled(*args):pass
        with pytest.raises(ToolProtocolError) as released:
            await ex.prepare(follow,p,'responses',b,pending_thread=tid,tool_sessions=SimpleNamespace(cancel_thread=cancelled))
        assert released.value.code=='tool_binding_invalidated'
        c=audit_for(follow,str(uuid4()))
        # Account change invalidates all bindings and increments identity version.
        async with SessionLocal() as db:
            worker=await db.get(Worker,w.id)
            await update_account(worker,{'type':'chatgpt','email':'new@example.test'})
            assert worker.execution_generation==1
            await update_account(worker,{'type':'chatgpt','email':'new@example.test'})
            assert worker.execution_generation==1
            await db.commit()
        async with SessionLocal() as db:
            with pytest.raises(Exception) as error:
                await main.validate_pending_worker(SimpleNamespace(),t,db)
            assert error.value.code=='tool_worker_invalidated'
    with TestClient(main.app) as client:client.portal.call(run)


def test_orphan_wait_rebuilds_but_live_wait_blocks_and_partial_history_rejects():
    async def run():
        key,w,p,t=await seed();a,b,follow,rid,tid=await checkpoint(key,w,p,t,tool=True)
        live=SimpleNamespace(has_pending=lambda *a:True)
        with pytest.raises(HTTPException) as error:await ex.prepare(follow,p,'responses',b,tool_sessions=live)
        assert error.value.detail['error']['code']=='conversation_waiting_tool'
        partial=req();c=audit_for(partial,__import__('json').loads(b['body'])['client_metadata']['thread_id'])
        with pytest.raises(HTTPException) as error:await ex.prepare(partial,p,'responses',c)
        assert error.value.detail['error']['code']=='conversation_history_required'
        prepared,binding=await ex.prepare(follow,p,'responses',b,tool_sessions=SimpleNamespace(has_pending=lambda *a:False))
        assert binding is None and prepared.previous_response_id is None
        assert b['execution_decision']['reason']=='pending_tool_lost'
        assert prepared.input==follow.input
        await ex.cleanup(b)
    with TestClient(main.app) as client:client.portal.call(run)


def test_live_wait_with_completed_tool_output_and_new_user_rebuilds():
    async def run():
        key,w,p,t=await seed();a,b,follow,rid,tid=await checkpoint(key,w,p,t,tool=True)
        cancelled=[]
        async def cancel(*args):cancelled.append(args)
        tools=SimpleNamespace(
            has_pending=lambda *args: True,
            can_supersede_with_user_turn=lambda *args: True,
            cancel_thread=cancel,
        )
        prepared,binding=await ex.prepare(follow,p,'responses',b,tool_sessions=tools)
        assert binding is None and prepared.previous_response_id is None
        assert prepared.input==follow.input
        assert b['execution_decision']['reason']=='pending_tool_superseded'
        assert cancelled==[(p.key_id,tid)]
        await ex.cleanup(b)
    with TestClient(main.app) as client:client.portal.call(run)


def test_two_hour_display_does_not_delete_old_bindings_or_history():
    prefix=uuid4().hex
    async def setup():
        key,w,p,t=await seed()
        async with SessionLocal() as db:
            for name,age in [('old',3),('recent',1)]:
                rid=prefix+'-'+name
                db.add(UsageRecord(request_id=rid,api_key_id=key.id,worker_id=w.id,thread_id=rid,logical_conversation_id='conv_'+rid,model='test',endpoint='responses',status_code=200))
                db.add(ResponseBinding(response_id=rid,api_key_id=key.id,worker_id=w.id,thread_id=rid,last_used_at=ex.now()-timedelta(hours=age),expires_at=None))
            await db.commit()
    async def verify():
        async with SessionLocal() as db:assert (await db.get(ResponseBinding,prefix+'-old')).status=='active'
    with TestClient(main.app) as client:
        client.portal.call(setup)
        s=get_settings();client.post('/auth/login',data={'username':s.admin_username,'password':s.admin_password.get_secret_value()})
        page=client.get('/admin/sessions');assert page.status_code==200,page.text
        assert prefix+'-recent' in page.text and prefix+'-old' not in page.text
        assert 'TTL 到期' not in page.text and '最近 2 小时' in page.text
        history=client.get('/admin/history',params={'conversation':'conv_'+prefix+'-old'})
        assert prefix+'-old' in history.text
        client.portal.call(verify)


@pytest.mark.asyncio
@pytest.mark.parametrize('safe',[True,False])
async def test_failover_replays_full_input_only_before_turn_started(monkeypatch,safe):
    calls=[]
    r=req([{'role':'user','content':'old'},{'role':'assistant','content':'answer'},{'role':'user','content':'next'}],previous_response_id='old-thread')
    r._execution_auto_resume=True;r._execution_input_text='USER:\nnext';r._execution_input_items=[r.input[-1]]
    first=BackendTarget('k:first','ws://first','/tmp');second=BackendTarget('k:second','ws://second','/tmp')
    async def replacement(*a):return second
    monkeypatch.setattr(main,'retry_target',replacement)
    class Backend:
        async def complete(self,body,target):
            calls.append(target)
            if target==first:raise WorkerFailure('disconnected',safe_to_retry=safe)
            assert body.previous_response_id is None and body._execution_input_items is None
            assert 'old' in body.input_text() and 'next' in body.input_text()
            return BackendResult(text='ok',thread_id='new-thread')
    if safe:
        result,target=await main.complete_with_failover(r,Backend(),ApiPrincipal(None,'test'),first,allow_retry=True)
        assert result.thread_id=='new-thread' and target==second
    else:
        with pytest.raises(WorkerFailure):await main.complete_with_failover(r,Backend(),ApiPrincipal(None,'test'),first,allow_retry=True)
        assert calls==[first]


def test_late_usage_cannot_reactivate_released_binding_and_errors_are_audited():
    import time
    from codex_gateway.audit import current_audit
    from codex_gateway.auth import require_api_key
    async def run():
        key,w,p,t=await seed();a,b,follow,rid,tid=await checkpoint(key,w,p,t)
        await ex.prepare(follow,p,'responses',b)
        async with SessionLocal() as db:
            await invalidate_bindings(db,api_key_id=key.id,thread_id=tid,reason='administrator_released')
            await db.commit()
        token=current_audit.set(b)
        late='resp_'+uuid4().hex
        try:await main.save_usage(late,p,t,'gpt-6-sol',200,time.monotonic(),BackendResult(text='late',thread_id=tid),persist_binding=True)
        finally:current_audit.reset(token);await ex.cleanup(b)
        async with SessionLocal() as db:
            assert await db.get(ResponseBinding,late) is None
            assert (await db.get(ResponseBinding,rid)).status=='invalid'
        return p
    with TestClient(main.app) as client:
        principal=client.portal.call(run)
        async def auth():current_audit.get()['principal']=principal;return principal
        class Missing:
            def continuation_target(self,r,k):
                from codex_gateway.client_tools import ToolProtocolError
                raise ToolProtocolError('This call was lost','client_tool_call_unavailable')
        main.app.dependency_overrides[require_api_key]=auth
        main.app.dependency_overrides[main.get_backend]=lambda:Missing()
        try:
            response=client.post('/v1/responses',json={'model':'gpt-6-sol','input':[{'type':'function_call_output','call_id':'missing','output':'done'}]})
            assert response.status_code==400
            async def check():
                async with SessionLocal() as db:
                    r=await db.scalar(select(UsageRecord).where(UsageRecord.api_key_id==principal.key_id,UsageRecord.status_code==400).order_by(UsageRecord.created_at.desc()))
                    assert r.error_code=='client_tool_call_unavailable'
                    assert r.conversation_evidence['rejection']['message']=='This call was lost'
            client.portal.call(check)
        finally:
            main.app.dependency_overrides.pop(require_api_key,None)
            main.app.dependency_overrides.pop(main.get_backend,None)


@pytest.mark.asyncio
async def test_account_generation_failure_does_not_quarantine_worker(monkeypatch):
    async def forbidden(*args):pytest.fail('A healthy new account must not be quarantined')
    monkeypatch.setattr(main,'quarantine_worker',forbidden)
    class Backend:
        async def complete(self,*args):raise WorkerFailure('identity changed',kind='account_changed',safe_to_retry=True)
    with pytest.raises(WorkerFailure):
        await main.complete_with_failover(req(),Backend(),ApiPrincipal(None,'test'),BackendTarget('key:w','ws://test','/tmp',uuid4(),0),allow_retry=False)
