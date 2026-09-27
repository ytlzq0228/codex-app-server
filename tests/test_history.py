from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4
import re

from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import select

from codex_gateway.main import app
from codex_gateway.database import SessionLocal
from codex_gateway.history import conversation_history
from codex_gateway.models import ApiKey, ResponseBinding, UsageRecord, Worker
from codex_gateway.user_auth import require_user
from codex_gateway.config import get_settings


def test_conversation_pagination_latest_status_filters_and_owner_scope():
    prefix=uuid4().hex
    alice,bob=prefix+'-alice',prefix+'-bob'
    now=datetime.now(timezone.utc)
    tie_id=uuid4().int & ~1
    async def seed_and_check():
        async with SessionLocal() as db:
            key=ApiKey(name='history-test',prefix=prefix[:20],key_hash=prefix*2)
            db.add(key);await db.flush()
            def record(name,conv,status,age,owner=alice,**extra):
                return UsageRecord(request_id=prefix+'-'+name,owner_username=owner,
                    logical_conversation_id=conv,status_code=status,created_at=now+timedelta(seconds=age),
                    endpoint='responses',model='test-model',input_tokens=10,output_tokens=2,
                    duration_ms=5,cost_usd=Decimal('0.1'),**extra)
            db.add_all([
                record('old','recovered',502,0),record('new','recovered',200,1),
                record('other-owner','recovered',503,2,owner=bob),
                record('failing-old','failing',200,3),record('failing-new','failing',500,4),
                record('different-key','recovered',200,5,api_key_id=key.id),
                record('standalone',None,200,6),
                record('tie-low','tie',200,7,id=UUID(int=tie_id)),
                record('tie-high','tie',504,7,id=UUID(int=tie_id+1)),
            ])
            worker=await db.scalar(select(Worker).limit(1))
            legacy=record('legacy',None,200,8,api_key_id=key.id)
            db.add(legacy)
            db.add(ResponseBinding(response_id=legacy.request_id,api_key_id=key.id,worker_id=worker.id,thread_id='legacy-thread',expires_at=now+timedelta(hours=1)))
            await db.commit()
            result=await conversation_history(db,owner=alice)
            assert result['total']==6 and result['request_total']==9
            groups=result['groups']
            recovered=next(g for g in groups if g['thread_id']=='recovered' and len(g['requests'])==2)
            assert recovered['latest_status']==200
            assert recovered['input_tokens']==20 and recovered['cost_usd']==Decimal('0.2')
            assert [r['usage'].status_code for r in recovered['requests']]==[502,200]
            assert next(g for g in groups if g['thread_id']=='tie')['latest_status']==504
            assert groups[0]['thread_id']=='legacy-thread'
            assert all(r['usage'].owner_username==alice for g in groups for r in g['requests'])
            filtered=await conversation_history(db,owner=alice,filters=[UsageRecord.request_id==prefix+'-old'])
            assert filtered['total']==1 and filtered['groups'][0]['latest_status']==200
            assert len(filtered['groups'][0]['requests'])==2
            assert (await conversation_history(db,owner=alice,status='error'))['total']==2
            assert (await conversation_history(db,owner=alice,status='error',filters=[UsageRecord.request_id==prefix+'-old']))['total']==0
            pages=[await conversation_history(db,owner=alice,page=p,page_size=1) for p in range(1,7)]
            assert len({p['groups'][0]['identity'] for p in pages})==6
            assert sum(len(p['groups'][0]['requests']) for p in pages)==9
            assert (await conversation_history(db,owner=alice,page=99,page_size=1))['page']==6
    async def signed_in(request: Request):
        request.state.user=SimpleNamespace(role='user',username=alice)
        return SimpleNamespace(username=alice,csrf_token='test')
    with TestClient(app) as client:
        client.portal.call(seed_and_check)
        app.dependency_overrides[require_user]=signed_in
        try:
            response=client.get('/user/usage',params={'q':prefix+'-old'})
            assert response.status_code==200,response.text
            assert '共 1 个会话、2 条请求' in response.text
            top=re.search(r'<tr class="history-row".*?</tr>',response.text,re.S)[0]
            assert 'badge-ok' in top and '>200</span>' in top and '502' not in top
            assert prefix+'-old' in response.text and prefix+'-new' in response.text
            assert prefix+'-other-owner' not in response.text
            assert client.get('/user/usage/'+prefix+'-other-owner').status_code==404
            assert '共 0 个会话' in client.get('/user/usage',params={'q':prefix+'-old','status':'error'}).text
            assert 'data-toggle-history' in response.text and '/static/history.js' in response.text
        finally:app.dependency_overrides.pop(require_user,None)
        settings=get_settings()
        assert client.post('/auth/login',data={'username':settings.admin_username,'password':settings.admin_password.get_secret_value()},follow_redirects=False).status_code==302
        admin=client.get('/admin/history')
        assert admin.status_code==200,admin.text
        assert '含失败请求' not in admin.text
        assert '最近请求状态' in admin.text


def test_active_conversation_grouping_does_not_cross_keys_or_interfaces():
    from codex_gateway.history import active_conversation_groups
    now=datetime.now(timezone.utc)
    key=SimpleNamespace(id=uuid4()); other=SimpleNamespace(id=uuid4())
    worker=SimpleNamespace(id=uuid4(),name='worker')
    def row(key,thread,logical='conv-a',endpoint='responses',missing=False):
        binding=SimpleNamespace(thread_id=thread,last_used_at=now)
        usage=None if missing else SimpleNamespace(logical_conversation_id=logical,thread_id=thread,endpoint=endpoint)
        return binding,key,worker,usage.logical_conversation_id if usage else None,usage.thread_id if usage else None,usage.endpoint if usage else None
    groups,by_key=active_conversation_groups([
        row(key,'t2'),row(key,'t2'),row(key,'t1'),row(other,'t1'),
        row(key,'t1',endpoint='chat.completions'),row(key,'legacy',missing=True)])
    assert len(groups)==4 and len(by_key[key.id])==3
    group=groups[0]
    assert group['conversation_id']=='conv-a' and group['binding_count']==3
    assert len(group['threads'])==2 and group['threads'][0]['binding_count']==2
    assert 'key_id='+str(key.id) in group['history_url']
    assert groups[-1]['conversation_id']=='legacy' and not groups[-1]['logical']


def test_active_and_historical_pages_share_logical_id_and_keep_thread_actions():
    prefix=uuid4().hex
    conv='conv_'+prefix
    now=datetime.now(timezone.utc)
    async def seed():
        async with SessionLocal() as db:
            key=ApiKey(name=prefix,prefix=prefix[:20],key_hash=prefix*2)
            other=ApiKey(name=prefix+'other',prefix=prefix[:19]+'x',key_hash=prefix[::-1]*2)
            db.add_all([key,other]);await db.flush()
            worker=await db.scalar(select(Worker).limit(1))
            for i in range(13):
                thread='thread-'+prefix+('-a' if i<5 else '-b')
                rid=prefix+'-'+str(i)
                db.add(UsageRecord(request_id=rid,api_key_id=key.id,logical_conversation_id=conv,
                    thread_id=thread,worker_id=worker.id,model='test-model',status_code=200,
                    endpoint='responses',created_at=now+timedelta(seconds=i)))
                db.add(ResponseBinding(response_id=rid,api_key_id=key.id,worker_id=worker.id,
                    thread_id=thread,last_used_at=now+timedelta(seconds=i),expires_at=now+timedelta(hours=1)))
            db.add(UsageRecord(request_id=prefix+'-unrelated',api_key_id=other.id,logical_conversation_id=conv,
                thread_id='unrelated-thread',model='test-model',status_code=200,endpoint='responses'))
            await db.commit()
            return str(key.id)
    with TestClient(app) as client:
        key_id=client.portal.call(seed)
        s=get_settings()
        assert client.post('/auth/login',data={'username':s.admin_username,'password':s.admin_password.get_secret_value()},follow_redirects=False).status_code==302
        active=client.get('/admin/sessions');assert active.status_code==200,active.text
        assert conv in active.text and '2 个 Thread · 13 条活动响应绑定' in active.text
        assert '释放 Thread' in active.text
        focused=client.get('/admin/sessions',params={'conversation':conv,'key_id':key_id,'endpoint':'responses'})
        assert focused.status_code==200 and '2 个 Thread · 13 条活动响应绑定' in focused.text
        assert 'class="active-conversation" open' in focused.text
        history=client.get('/admin/history',params={'conversation':conv,'key_id':key_id,'endpoint':'responses'})
        assert history.status_code==200,history.text
        assert '共 13 条请求，聚合为 1 个会话' in history.text
        assert '2 个 Worker Thread' in history.text
        assert 'unrelated-thread' not in history.text
        token=re.search(r'name="csrf_token" value="([^"]+)"',active.text)[1]
        deleted=client.post('/admin/sessions/'+prefix+'-0/delete',data={'csrf_token':token},headers={'X-Requested-With':'XMLHttpRequest'})
        assert deleted.status_code==200,deleted.text
        active=client.get('/admin/sessions')
        assert '1 个 Thread · 8 条活动响应绑定' in active.text
        assert '共 13 条请求，聚合为 1 个会话' in client.get('/admin/history',params={'conversation':conv,'key_id':key_id}).text
