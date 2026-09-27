import asyncio
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from codex_gateway import execution as ex
from codex_gateway.auth import ApiPrincipal, require_api_key
from codex_gateway.audit import current_audit
from codex_gateway.backend import BackendTarget, AppServerBackend
from codex_gateway.database import SessionLocal
from codex_gateway.main import app
from codex_gateway.models import ApiKey, ExecutionSession, ResponseBinding, UsageRecord, Worker
from codex_gateway.schemas import ResponseRequest, BackendResult


def req(items=None, **kwargs):
    return ResponseRequest(model='gpt-6-sol',input=items or [{'role':'user','content':'hello'}],**kwargs)


def audit_for(request, thread='client-thread'):
    return {'body':json.dumps({**request.model_dump(mode='json'),'client_metadata':{'thread_id':thread}}).encode(),
            'transport':{'headers':[]},'body_hash':hashlib.sha256(), 'body_bytes_received':1,'body_complete':True}


def test_append_only_canonicalisation_and_private_override():
    first=req()
    expected=ex.hashes(ex.history_items(first))+[ex.digest({'role':'assistant','text':'answer'})]
    items=[{'type':'message','role':'user','content':[{'type':'input_text','text':'hello'}]},
           {'id':'msg_ignored','type':'message','role':'assistant','phase':'final_answer',
            'content':[{'type':'output_text','text':'answer','annotations':[]}]},
           {'role':'user','content':'next'}]
    follow=req(items)
    assert ex.appended_items(follow,expected)==[items[-1]]
    assert ex.appended_items(req(items[:-1]),expected) is None
    assert ex.appended_items(req([{'role':'user','content':'edited'},*items[1:]]),expected) is None
    assert ex.appended_items(req('incremental'),expected) is None
    hostile=req(_execution_input_text='INJECTED',_execution_auto_resume=True)
    assert hostile.input_text()=='USER:\nhello' and not hostile._execution_auto_resume


def test_persistent_lease_checkpoint_and_isolation():
    async def run():
        async with SessionLocal() as db:
            k=ApiKey(name='execution-test',prefix=uuid4().hex[:20],key_hash=uuid4().hex*2)
            db.add(k);await db.commit()
            w=await db.scalar(select(Worker).limit(1))
            principal=ApiPrincipal(k.id,'test')
            target=BackendTarget(str(k.id)+':'+str(w.id),w.endpoint,'/workspace',w.id)
        first=req(); a=audit_for(first,str(uuid4()))
        prepared,binding=await ex.prepare(first,principal,'responses',a)
        assert binding is None and a['execution_decision']['reason']=='no_checkpoint'
        second_a={**a};second_a.pop('execution');second_a.pop('execution_decision')
        with pytest.raises(HTTPException) as busy:
            await ex.prepare(first,principal,'responses',second_a)
        assert busy.value.status_code==409
        response_id='resp_'+uuid4().hex
        async with SessionLocal() as db:
            db.add(ResponseBinding(response_id=response_id,api_key_id=k.id,worker_id=w.id,thread_id='thread-1',expires_at=ex.now()+timedelta(hours=1)))
            await ex.finish(db,a,BackendResult(text='answer',thread_id='thread-1'),target,response_id)
            await db.commit()
        await ex.cleanup(a)
        following=req([*first.input,{'role':'assistant','content':'answer'},{'role':'user','content':'next'}])
        thread=json.loads(a['body'])['client_metadata']['thread_id']
        follow_a=audit_for(following,thread)
        prepared,binding=await ex.prepare(following,principal,'responses',follow_a)
        assert binding.thread_id=='thread-1' and prepared.previous_response_id=='thread-1'
        assert prepared.input_text()=='USER:\nnext'
        assert prepared.input==following.input
        assert follow_a['execution_decision']['action']=='resume'
        # A stale owner cannot clear a newer lease.
        await ex.cleanup(a)
        async with SessionLocal() as db:
            row=await db.get(ExecutionSession,follow_a['execution']['logical_id'])
            assert row.lease_token==follow_a['execution']['token']
        await ex.cleanup(follow_a)
        recovered=audit_for(following,thread)
        prepared,binding=await ex.prepare(following,principal,'responses',recovered)
        assert binding is None and recovered['execution_decision']['reason']=='previous_execution_incomplete'
        assert prepared.input_text()==following.input_text()
        await ex.cleanup(recovered)
        other=audit_for(following,thread)
        _,binding=await ex.prepare(following,principal,'chat.completions',other)
        assert binding is None and other['execution']['logical_id']!=a['execution']['logical_id']
        await ex.cleanup(other)
    with TestClient(app) as client:client.portal.call(run)


@pytest.mark.parametrize('change,reason',[
    ('edit','history_not_append_only'),('tools','configuration_changed'),
    ('released','binding_invalidated'),('worker','worker_unavailable'),
])
def test_safe_rollovers(change,reason):
    async def run():
        async with SessionLocal() as db:
            k=ApiKey(name='rollover',prefix=uuid4().hex[:20],key_hash=uuid4().hex*2)
            w=Worker(name='resume-'+uuid4().hex,container_name='test-'+uuid4().hex,endpoint='ws://test',enabled=True,status='ready')
            db.add_all([k,w]);await db.commit()
        p=ApiPrincipal(k.id,'test');t=BackendTarget(str(k.id)+':'+str(w.id),w.endpoint,'/tmp',w.id)
        r=req();thread=str(uuid4());a=audit_for(r,thread)
        await ex.prepare(r,p,'responses',a)
        rid='resp_'+uuid4().hex
        async with SessionLocal() as db:
            db.add(ResponseBinding(response_id=rid,api_key_id=k.id,worker_id=w.id,thread_id='t',expires_at=ex.now()+timedelta(hours=1)))
            await ex.finish(db,a,BackendResult(text='answer',thread_id='t'),t,rid)
            await db.commit()
        await ex.cleanup(a)
        follow=req([*r.input,{'role':'assistant','content':'answer'},{'role':'user','content':'next'}])
        if change=='edit':follow.input[0]={'role':'user','content':'edited'}
        if change=='tools':follow.tools=[{'type':'function','name':'new','parameters':{'type':'object'}}]
        async with SessionLocal() as db:
            if change=='expired':await db.execute(update(ExecutionSession).where(ExecutionSession.logical_id==a['execution']['logical_id']).values(expires_at=ex.now()-timedelta(seconds=1)))
            if change=='released':await db.execute(update(ResponseBinding).where(ResponseBinding.response_id==rid).values(status='deleted'))
            if change=='worker':await db.execute(update(Worker).where(Worker.id==w.id).values(enabled=False))
            await db.commit()
        b=audit_for(follow,thread)
        prepared,binding=await ex.prepare(follow,p,'responses',b)
        assert binding is None and prepared.previous_response_id is None
        assert b['execution_decision']['reason']==reason
        await ex.cleanup(b)
    with TestClient(app) as client:client.portal.call(run)


def test_http_two_turns_use_one_thread_and_audit_original_history(monkeypatch):
    async def seed():
        async with SessionLocal() as db:
            k=ApiKey(name='http-resume',prefix=uuid4().hex[:20],key_hash=uuid4().hex*2)
            db.add(k);await db.commit()
            return k.id
    with TestClient(app) as client:
        key=client.portal.call(seed)
        async def auth():
            p=ApiPrincipal(key,'test')
            current_audit.get()['principal']=p
            return p
        app.dependency_overrides[require_api_key]=auth
        try:
            headers={'thread-id':str(uuid4()),'originator':'test-client'}
            first=[{'role':'user','content':'first'}]
            r1=client.post('/v1/responses',headers=headers,json={'model':'gpt-6-sol','input':first})
            assert r1.status_code==200,r1.text
            output=r1.json()['output'][0]['content'][0]['text']
            full=[*first,{'role':'assistant','content':output},{'role':'user','content':'second'}]
            r2=client.post('/v1/responses',headers=headers,json={'model':'gpt-6-sol','input':full})
            assert r2.status_code==200,r2.text
            assert r2.json()['output'][0]['content'][0]['text']=='mock: USER:\nsecond'
            assert r2.json()['previous_response_id'] is None
            async def verify():
                async with SessionLocal() as db:
                    records=list((await db.scalars(select(UsageRecord).where(UsageRecord.request_id.in_([r1.json()['id'],r2.json()['id']])).order_by(UsageRecord.created_at))).all())
                    assert len(records)==2 and records[0].thread_id==records[1].thread_id
                    assert records[0].worker_id==records[1].worker_id
                    assert records[1].request_params['input']==full
                    assert records[1].conversation_evidence['execution']['action']=='resume'
            client.portal.call(verify)
        finally:app.dependency_overrides.pop(require_api_key,None)

@pytest.mark.parametrize('endpoint', ['responses','chat.completions'])
def test_streaming_continuation_and_pending_tools(endpoint):
    from codex_gateway.tool_sessions import ToolSessions
    from codex_gateway.schemas import BackendStreamEvent
    from codex_gateway.main import get_backend
    from codex_gateway.backend import MockBackend
    calls=[]
    class Backend(MockBackend):
        def __init__(self): self.tool_sessions=ToolSessions(self.events)
        def continuation_target(self,r,k):return self.tool_sessions.target_for(r,k)
        def continuation_thread(self,r,k):
            run=self.tool_sessions.find(r,k)
            return run.thread_id if run else None
        async def events(self,r,t,run):
            thread=r.previous_response_id or 'tool-thread-'+uuid4().hex
            calls.append((thread,r.input_text(),t.worker_id))
            call={'type':'function_call','id':'fc_'+uuid4().hex,'call_id':'call_'+uuid4().hex,'name':'lookup','arguments':'{}','status':'completed'}
            await self.tool_sessions.await_result(run,call)
            yield BackendStreamEvent(tool_call=call,thread_id=thread)
            output=await self.tool_sessions.receive_result(run)
            yield BackendStreamEvent(delta='answer '+output,thread_id=thread)
            yield BackendStreamEvent(done=True,thread_id=thread)
        async def stream(self,r,t):
            async for e in self.tool_sessions.stream(r,t):yield e
        async def close(self):await self.tool_sessions.close()
    async def seed():
        async with SessionLocal() as db:
            key=ApiKey(name='stream-tools',prefix=uuid4().hex[:20],key_hash=uuid4().hex*2)
            db.add(key);await db.commit();return key.id
    backend=Backend()
    with TestClient(app) as client:
        key=client.portal.call(seed)
        async def auth():
            p=ApiPrincipal(key,'test');current_audit.get()['principal']=p;return p
        app.dependency_overrides[require_api_key]=auth
        app.dependency_overrides[get_backend]=lambda:backend
        try:
            headers={'thread-id':str(uuid4())}
            tool={'type':'function','name':'lookup','parameters':{'type':'object'}}
            url='/v1/responses' if endpoint=='responses' else '/v1/chat/completions'
            history=[{'role':'user','content':'first'}]
            def send(history,headers=headers):
                if endpoint=='responses':payload={'input':history,'tools':[tool]}
                else:payload={'messages':history,'tools':[{'type':'function','function':{k:v for k,v in tool.items() if k!='type'}}]}
                res=client.post(url,headers=headers,json={'model':'gpt-6-sol','stream':True,**payload})
                assert res.status_code==200,res.text
                return [json.loads(line[6:]) for line in res.text.splitlines() if line.startswith('data: ') and line!='data: [DONE]']
            events=send(history)
            if endpoint=='responses':
                call=events[-1]['response']['output'][0]
                history.extend([call,{'type':'function_call_output','call_id':call['call_id'],'output':'ok'}])
            else:
                call=next(e['choices'][0]['delta']['tool_calls'][0] for e in events if e.get('choices') and e['choices'][0]['delta'].get('tool_calls'))
                history.extend([{'role':'assistant','tool_calls':[call]},{'role':'tool','tool_call_id':call['id'],'content':'ok'}])
            # New user turn cannot displace a suspended tool run.
            payload={'input':[{'role':'user','content':'other'}]} if endpoint=='responses' else {'messages':[{'role':'user','content':'other'}]}
            busy=client.post(url,headers=headers,json={'model':'gpt-6-sol',**payload})
            assert busy.status_code==409,busy.text
            # Metadata may be absent on tool return; authenticated call_id recovers it.
            events=send(history,headers={})
            if endpoint=='responses':assert events[-1]['response']['output'][0]['content'][0]['text']=='answer ok'
            else:assert any(e.get('choices') and e['choices'][0]['delta'].get('content')=='answer ok' for e in events)
            history.extend([{'role':'assistant','content':'answer ok'},{'role':'user','content':'second'}])
            send(history)
            assert len(calls)==2 and calls[0][0]==calls[1][0] and calls[0][2]==calls[1][2]
            assert calls[1][1]=='USER:\nsecond'
        finally:
            client.portal.call(backend.close)
            app.dependency_overrides.pop(get_backend,None)
            app.dependency_overrides.pop(require_api_key,None)


@pytest.mark.asyncio
async def test_auto_resume_failure_rebuilds_full_history_but_explicit_failure_does_not():
    from codex_gateway.config import Settings
    from codex_gateway.app_server import AppServerError
    from codex_gateway.backend import WorkerFailure
    class Server:
        calls=[]
        async def call(self,method,params):
            self.calls.append(method)
            if method=='thread/resume':raise AppServerError('thread not found')
            return {'thread':{'id':'replacement'}}
    backend=AppServerBackend(Settings())
    r=req(previous_response_id='old')
    r._execution_input_text='delta';r._execution_auto_resume=True
    server=Server()
    assert await backend._start_thread(server,r,'/workspace')=='replacement'
    assert r.input_text()=='USER:\nhello' and r.previous_response_id is None
    assert server.calls==['thread/resume','thread/start']
    with pytest.raises(WorkerFailure):await backend._start_thread(Server(),req(previous_response_id='old'),'/workspace')
    await backend.close()


def test_usage_excludes_prior_turn_and_duplicate_notifications():
    from codex_gateway.backend import TurnUsage
    usage=TurnUsage()
    def event(total,last):
        return {'tokenUsage':{'total':{'inputTokens':total,'outputTokens':total//10},
                             'last':{'inputTokens':last,'outputTokens':last//10}}}
    usage.observe(event(1200,200))
    usage.observe(event(1200,200))
    usage.observe(event(1500,300))
    assert usage.counts[:2]==(500,50)


@pytest.mark.asyncio
async def test_worker_ignores_stale_turn_events():
    from contextlib import asynccontextmanager
    from codex_gateway.config import Settings
    class Server:
        async def call(self,method,params):return {'thread':{'id':'t'},'turn':{'id':'current'}}
        async def messages(self):
            yield {'method':'item/agentMessage/delta','params':{'threadId':'other','turnId':'current','delta':'WRONG'}}
            yield {'method':'turn/completed','params':{'threadId':'t','turn':{'id':'old','status':'completed'}}}
            yield {'method':'thread/tokenUsage/updated','params':{'threadId':'t','turnId':'old','tokenUsage':{'total':{'inputTokens':999}}}}
            yield {'method':'item/agentMessage/delta','params':{'threadId':'t','turnId':'current','delta':'correct'}}
            yield {'method':'turn/completed','params':{'threadId':'t','turn':{'id':'current','status':'completed'}}}
    class Pool:
        @asynccontextmanager
        async def lease(self,*a):yield Server(),0
        async def close(self):pass
    backend=AppServerBackend(Settings());backend.pool=Pool()
    result=await backend.complete(req(),BackendTarget('k:w','ws://test','/workspace'))
    assert result.text=='correct' and result.input_tokens==0
    await backend.close()


def test_explicit_delta_does_not_become_full_history_checkpoint():
    async def run():
        async with SessionLocal() as db:
            key=ApiKey(name='explicit-test',prefix=uuid4().hex[:20],key_hash=uuid4().hex*2)
            db.add(key);await db.commit()
            worker=await db.scalar(select(Worker).where(Worker.name=='worker-1'))
            binding=ResponseBinding(response_id='resp_'+uuid4().hex,api_key_id=key.id,worker_id=worker.id,
                thread_id='explicit-'+uuid4().hex,expires_at=ex.now()+timedelta(hours=1))
            db.add(binding);await db.commit()
        r=req(previous_response_id=binding.thread_id);a=audit_for(r,str(uuid4()))
        prepared,chosen=await ex.prepare(r,ApiPrincipal(key.id,'test'),'responses',a,binding=binding)
        assert chosen==binding and prepared.previous_response_id==binding.thread_id
        assert a['execution']['history'] is None
        assert a['execution_decision']['action']=='explicit_resume'
        await ex.cleanup(a)
    with TestClient(app) as client:client.portal.call(run)
