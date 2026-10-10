from page_helpers import rendered_pages
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
                    endpoint='responses',model='model-'+name,input_tokens=10,output_tokens=2,
                    duration_ms=5,cost_usd=Decimal('0.1'),
                    request_observation={'headers': [{'name': 'User-Agent', 'value': 'agent-'+name}]}
                        if name != 'failing-new' else None,**extra)
            db.add_all([
                record('old','recovered',502,0),record('new','recovered',200,1),
                record('other-owner','recovered',503,2,owner=bob),
                record('failing-old','failing',200,3),record('failing-new','failing',500,4),
                record('different-key','recovered',200,5,api_key_id=key.id),
                record('standalone',None,499,6),
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
            assert recovered['latest_model']=='model-new'
            assert recovered['latest_status']==200
            assert recovered['input_tokens']==20 and recovered['cost_usd']==Decimal('0.2')
            assert recovered['unpriced_count']==0
            assert [r['usage'].status_code for r in recovered['requests']]==[200,502]
            assert next(g for g in groups if g['thread_id']=='tie')['latest_status']==504
            assert groups[0]['thread_id']=='legacy-thread'
            summaries=await conversation_history(db,owner=alice,summaries_only=True,include_user_agent=True)
            assert next(g for g in summaries['groups'] if g['conversation_id']=='tie')['latest_model']=='model-tie-high'
            assert next(g for g in summaries['groups'] if g['conversation_id']=='tie')['latest_user_agent']=='agent-tie-high'
            assert next(g for g in summaries['groups'] if g['conversation_id']=='failing')['latest_user_agent']==''
            filtered_summary=await conversation_history(db,owner=alice,summaries_only=True,
                include_user_agent=True,filters=[UsageRecord.request_id==prefix+'-old'])
            assert filtered_summary['groups'][0]['latest_user_agent']=='agent-new'
            assert all(r['usage'].owner_username==alice for g in groups for r in g['requests'])
            filtered=await conversation_history(db,owner=alice,filters=[UsageRecord.request_id==prefix+'-old'])
            assert filtered['total']==1 and filtered['groups'][0]['latest_status']==200
            assert len(filtered['groups'][0]['requests'])==2
            assert (await conversation_history(db,owner=alice,status='error'))['total']==2
            assert (await conversation_history(db,owner=alice,status='success'))['total']==4
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
            response=client.get('/user/usage/data',params={'q':prefix+'-old'})
            assert response.status_code==200,response.text
            history=response.json()['history']
            assert history['total']==1 and history['request_total']==2
            group=history['groups'][0]
            assert group['latest_model']=='model-new'
            assert group['latest_status']==200 and 'requests' not in group
            assert Decimal(group['cost_usd'])==Decimal('0.2')
            batch=client.get('/user/usage/requests',params={'conversation':group['conversation_id'],
                'key_id':group['key_id'] or 'development','endpoint':group['endpoint']}).json()
            assert {r['request_id'] for r in batch['requests']}=={prefix+'-old',prefix+'-new'}
            assert client.get('/user/usage/'+prefix+'-other-owner'+'/data').status_code==404
            assert client.get('/user/usage/data',params={'q':prefix+'-old','status':'error'}).json()['history']['total']==0
            cancelled=client.get('/user/usage/data',params={'q':prefix+'-standalone'}).json()['history']
            assert cancelled['groups'][0]['latest_status']==499
            assert client.get('/user/usage/data',params={'q':prefix+'-standalone','status':'error'}).json()['history']['total']==0
        finally:app.dependency_overrides.pop(require_user,None)
        settings=get_settings()
        assert client.post('/auth/login',data={'username':settings.admin_username,'password':settings.admin_password.get_secret_value()},follow_redirects=False).status_code in {302,303}
        admin=rendered_pages(client, '/admin/history')
        assert admin.status_code==200,admin.text
        assert '含失败请求' not in admin.text
        assert '/static/admin-history.js' in admin.text
        assert 'data-history-results' in admin.text
        assert '<body data-csrf-token=' in admin.text and 'class="admin-page ' in admin.text
        assert 'class="admin-page ' in rendered_pages(client, '/admin/users').text


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
            for i in range(25):
                thread='thread-'+prefix+('-a' if i<10 else '-b')
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
        assert client.post('/auth/login',data={'username':s.admin_username,'password':s.admin_password.get_secret_value()},follow_redirects=False).status_code in {302,303}
        active=rendered_pages(client, '/admin/sessions');assert active.status_code==200,active.text
        assert conv in active.text and 'thread-'+prefix+'-a' in active.text and 'thread-'+prefix+'-b' in active.text
        assert '释放 Thread' in active.text
        focused=rendered_pages(client, '/admin/sessions',params={'conversation':conv,'key_id':key_id,'endpoint':'responses'})
        assert focused.status_code==200 and focused.text.count('>释放 Thread</button>')==2
        assert '<details' not in focused.text
        history=rendered_pages(client, '/admin/history',params={'conversation':conv,'key_id':key_id,'endpoint':'responses'})
        assert history.status_code==200,history.text
        assert prefix+'-24' not in history.text
        params={'conversation':conv,'key_id':key_id,'endpoint':'responses'}
        summary=rendered_pages(client, '/admin/history/data',params=params).json()
        assert summary['request_total']==25 and summary['total']==1
        assert summary['groups'][0]['thread_count']==2
        assert 'requests' not in summary['groups'][0]
        first=rendered_pages(client, '/admin/history/requests',params=params).json()
        second=rendered_pages(client, '/admin/history/requests',params={**params,'page':2}).json()
        assert len(first['requests'])==20 and len(second['requests'])==5
        assert first['requests'][0]['request_id']==prefix+'-24'
        assert second['requests'][-1]['request_id']==prefix+'-0'
        assert 'unrelated-thread' not in str(first)+str(second)
        token=re.search(r'name="csrf_token" value="([^"]+)"',active.text)[1]
        deleted=client.post('/admin/sessions/'+prefix+'-0/delete',data={'csrf_token':token},headers={'X-Requested-With':'XMLHttpRequest'})
        assert deleted.status_code==200,deleted.text
        active=rendered_pages(client, '/admin/sessions')
        assert 'thread-'+prefix+'-b' in active.text and 'thread-'+prefix+'-a' not in active.text
        assert rendered_pages(client, '/admin/history/data',params={'conversation':conv,'key_id':key_id}).json()['request_total']==25


def test_history_time_bounds_require_timezone_and_normalize_to_utc():
    import pytest
    from fastapi import HTTPException
    from codex_gateway.history import history_time_filters
    filters = history_time_filters('2026-09-27T08:00:00+08:00', '2026-09-27T01:00:00Z')
    assert filters[0].right.value == datetime(2026, 9, 27, tzinfo=timezone.utc)
    assert filters[1].right.value == datetime(2026, 9, 27, 1, tzinfo=timezone.utc)
    assert str(filters[0].operator.__name__) == 'ge'
    assert str(filters[1].operator.__name__) == 'lt'
    assert history_time_filters() == []
    assert len(history_time_filters(end='2026-09-27T01:00:00Z')) == 1
    for start, end in [
        ('2026-09-27T08:00:00', ''), ('invalid', ''),
        ('2026-09-27T01:00:00Z', '2026-09-27T00:00:00Z'),
        ('2026-09-27T01:00:00Z', '2026-09-27T01:00:00Z'),
    ]:
        with pytest.raises(HTTPException) as error:
            history_time_filters(start, end)
        assert error.value.status_code == 400


def test_admin_history_time_filter_preserves_full_conversation():
    prefix=uuid4().hex
    now=datetime(2026, 9, 27, tzinfo=timezone.utc)
    async def seed():
        async with SessionLocal() as db:
            key=ApiKey(name=prefix,prefix=prefix[:20],key_hash=prefix*2)
            db.add(key)
            await db.flush()
            for i in range(3):
                db.add(UsageRecord(request_id=prefix+str(i),api_key_id=key.id,
                    logical_conversation_id=prefix,model='test',status_code=200,
                    endpoint='responses',created_at=now+timedelta(seconds=i)))
            await db.commit()
            return str(key.id)
    with TestClient(app) as client:
        key_id=client.portal.call(seed)
        settings=get_settings()
        client.post('/auth/login',data={'username':settings.admin_username,
            'password':settings.admin_password.get_secret_value()})
        params={'conversation':prefix,'key_id':key_id,
            'start':'2026-09-27T08:00:01+08:00','end':'2026-09-27T00:00:02Z'}
        result=rendered_pages(client, '/admin/history',params=params)
        assert result.status_code==200
        data=rendered_pages(client, '/admin/history/data',params=params).json()
        assert data['request_total']==3 and data['total']==1
        assert 'data-key-select' in result.text and 'data-time-bound="start"' in result.text
        requests=rendered_pages(client, '/admin/history/requests',params={'conversation':prefix,'key_id':key_id,'endpoint':'responses'}).json()
        assert requests['requests'][-1]['created_at']=='2026-09-27T00:00:00+00:00'
        params['start']='2026-09-27T00:00:03Z'
        params.pop('end')
        assert rendered_pages(client, '/admin/history/data',params=params).json()['request_total']==0
        assert rendered_pages(client, '/admin/history',params={'start':'2026-09-27T00:00:00'}).status_code==400


def test_admin_json_pagination_is_bounded_and_does_not_transmit_audit_bodies():
    prefix=uuid4().hex
    now=datetime.now(timezone.utc)
    async def seed():
        async with SessionLocal() as db:
            key=ApiKey(name=prefix,prefix=prefix[:20],key_hash=prefix*2)
            db.add(key);await db.flush()
            for i in range(31):
                db.add(UsageRecord(request_id=prefix+'-group-'+str(i),api_key_id=key.id,
                    logical_conversation_id=prefix+'-'+str(i),model='<script>alert(1)</script>',
                    status_code=200,endpoint='responses',created_at=now+timedelta(seconds=i),
                    request_params={'private':'large-hidden-audit-body'},cost_usd=Decimal('0.0001')))
            for i in range(40):
                db.add(UsageRecord(request_id=prefix+'-detail-'+str(i),api_key_id=key.id,
                    logical_conversation_id=prefix+'-30',model='test',status_code=200,
                    endpoint='responses',created_at=now+timedelta(minutes=1,seconds=i),
                    request_params={'private':'large-hidden-audit-body'}))
            await db.commit();return str(key.id)
    with TestClient(app) as client:
        assert rendered_pages(client, '/admin/history/data',follow_redirects=False).status_code in {302,303}
        assert rendered_pages(client, '/admin/history/requests',params={'conversation':'x','key_id':'development','endpoint':'responses'},follow_redirects=False).status_code in {302,303}
        key_id=client.portal.call(seed)
        s=get_settings();client.post('/auth/login',data={'username':s.admin_username,'password':s.admin_password.get_secret_value()})
        shell=rendered_pages(client, '/admin/history',params={'key_id':key_id})
        assert prefix+'-group-' not in shell.text and prefix+'-detail-' not in shell.text
        first=rendered_pages(client, '/admin/history/data',params={'key_id':key_id})
        second=rendered_pages(client, '/admin/history/data',params={'key_id':key_id,'history_page':2}).json()
        data=first.json()
        assert first.headers['content-type'].startswith('application/json')
        assert first.headers['cache-control']=='no-store'
        assert len(data['groups'])==30 and len(second['groups'])==1
        assert data['request_total']==71 and data['total']==31
        assert not set(g['identity'] for g in data['groups']) & set(g['identity'] for g in second['groups'])
        assert data['groups'][0]['request_count']==41
        assert all('requests' not in group for group in data['groups'])
        params={'conversation':prefix+'-30','key_id':key_id,'endpoint':'responses'}
        pages=[rendered_pages(client, '/admin/history/requests',params={**params,'page':page}) for page in range(1,4)]
        assert [len(page.json()['requests']) for page in pages]==[20,20,1]
        assert len({row['request_id'] for page in pages for row in page.json()['requests']})==41
        assert 'large-hidden-audit-body' not in first.text+''.join(p.text for p in pages)
        assert data['groups'][-1]['cost_usd']=='0.0001'
        assert rendered_pages(client, '/admin/history/data',params={'history_page':0}).status_code==400
        assert rendered_pages(client, '/admin/history/requests',params={**params,'page':0}).status_code==400
        assert rendered_pages(client, '/admin/history/data',params={'start':'invalid'}).status_code==400
