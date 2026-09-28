import asyncio
import json
from types import SimpleNamespace
import pytest
from codex_gateway.client_tools import definitions,validate,public_call,ToolProtocolError
from codex_gateway.schemas import ResponseRequest,BackendStreamEvent,ChatCompletionRequest,BackendResult
from codex_gateway.backend import BackendTarget
from codex_gateway.tool_sessions import ToolSessions
from codex_gateway.worker_policy import thread_policy,turn_policy

TOOL={'type':'function','name':'lookup','parameters':{'type':'object','properties':{'query':{'type':'string'}}}}

def request(**kwargs):return ResponseRequest(model='test',input='hello',tools=[TOOL],**kwargs)


def test_namespace_alias_and_strict_unsupported_formats():
    r=ResponseRequest(model='test',input=[{'type':'additional_tools','tools':[{'type':'namespace','name':'functions','tools':[TOOL]}]},{'role':'user','content':'hello'}])
    specs=validate(r)
    call=public_call(specs,{'tool':'gateway_client_0','arguments':{'query':'dns'}})
    assert call['namespace']=='functions' and call['name']=='lookup'
    assert json.loads(call['arguments'])=={'query':'dns'}
    assert r.input_text()=='USER:\nhello'
    r.tools=[{'type':'custom','name':'exec','format':{'type':'grammar','syntax':'lark','definition':'start: /.+/'}}]
    assert r.unsupported() is None
    assert definitions(r)[0]['grammar']['syntax']=='lark'
    r.tools=[{'type':'local_shell','name':'shell'}]
    assert r.unsupported()


@pytest.mark.parametrize('field',['config','cwd','environments','permissions','sandbox','approvalPolicy'])
def test_cannot_override_worker_policy(field):
    r=request(**{field:'attacker'})
    with pytest.raises(ToolProtocolError):validate(r)
    assert thread_policy()['environments']==[]
    assert not thread_policy()['config']['features.shell_tool']
    assert not thread_policy()['config']['features.code_mode_host']
    assert turn_policy()['sandboxPolicy']=={'type':'readOnly','networkAccess':False}


def test_chat_tools_and_returned_result_conversion():
    r=ChatCompletionRequest(model='test',messages=[{'role':'tool','tool_call_id':'call_a','content':'answer'}],tools=[{'type':'function','function':{'name':'lookup','parameters':TOOL['parameters']}}])
    assert r.unsupported() is None
    assert r.to_response_request().input==[{'type':'function_call_output','call_id':'call_a','output':'answer'}]


@pytest.mark.asyncio
async def test_dynamic_continuation_is_scoped_and_not_reexecuted():
    sessions=None
    executions=[]
    async def events(req,target,run):
        executions.append('started')
        call={'id':'fc_a','type':'function_call','call_id':'call_a','name':'lookup','arguments':'{}','status':'completed'}
        await sessions.await_result(run,call)
        yield BackendStreamEvent(tool_call=call,thread_id='thread-a',input_tokens=10,output_tokens=1,cache_read_tokens=6,cache_write_tokens=2)
        result=await sessions.receive_result(run)
        yield BackendStreamEvent(delta=result,thread_id='thread-a')
        yield BackendStreamEvent(done=True,thread_id='thread-a',input_tokens=15,output_tokens=2,cache_read_tokens=9,cache_write_tokens=3)
    sessions=ToolSessions(events,ttl=1)
    target=BackendTarget('key:worker','ws://worker','/workspace')
    first=[e async for e in sessions.stream(request(),target)]
    assert first[0].tool_call['call_id']=='call_a'
    continued=ResponseRequest(model='test',input=[{'type':'function_call_output','call_id':'call_a','output':'client answer'}])
    with pytest.raises(ToolProtocolError):sessions.target_for(continued,'other-key')
    assert sessions.target_for(continued,'key')==target
    final=[e async for e in sessions.stream(continued,target)]
    assert final[0].delta=='client answer'
    assert (final[-1].input_tokens, final[-1].output_tokens, final[-1].cache_read_tokens, final[-1].cache_write_tokens)==(5,1,3,1)
    assert executions==['started']
    with pytest.raises(ToolProtocolError):sessions.target_for(continued,'key')
    await sessions.close()


@pytest.mark.asyncio
async def test_pending_output_followed_by_user_can_supersede_suspended_run():
    sessions = None
    async def events(req, target, run):
        call = {'call_id': 'call_superseded'}
        await sessions.await_result(run, call)
        yield BackendStreamEvent(tool_call=call, thread_id='thread-a')
        await sessions.receive_result(run)

    sessions = ToolSessions(events)
    target = BackendTarget('key:worker', 'ws://worker', '/workspace')
    assert [event async for event in sessions.stream(request(), target)][0].tool_call['call_id'] == 'call_superseded'
    for between in ([], [{'role':'assistant','content':'cached'}], [
            {'role':'assistant','content':'cached'}, {'role':'system','content':'policy'},
            {'role':'developer','content':'context'}]):
        combined = ResponseRequest(model='test', tools=[TOOL], input=[
            {'type':'additional_tools','tools':[TOOL]},
            {'role':'user','content':'checkpoint'},
            {'type':'custom_tool_call_output','call_id':'call_superseded','output':'done'},
            *between,
            {'role':'user','content':'next'},
        ])
        assert sessions.can_supersede_with_user_turn(combined, 'key', 'thread-a', 1)
    combined.input[2]['call_id'] = 'wrong'
    assert not sessions.can_supersede_with_user_turn(combined, 'key', 'thread-a', 1)
    combined.input[2]['call_id'] = 'call_superseded'
    assert not sessions.can_supersede_with_user_turn(combined, 'other-key', 'thread-a', 1)
    assert not sessions.can_supersede_with_user_turn(combined, 'key', 'other-thread', 1)
    # A matching output before the verified checkpoint is historical and cannot
    # authorize cancellation of the current suspended run.
    assert not sessions.can_supersede_with_user_turn(combined, 'key', 'thread-a', len(combined.input) - 1)
    await sessions.close()


@pytest.mark.asyncio
async def test_failed_continuation_reports_lost_call_instead_of_duplicate():
    sessions = None
    async def events(req, target, run):
        call = {'call_id': 'call_failed'}
        await sessions.await_result(run, call)
        yield BackendStreamEvent(tool_call=call, thread_id='thread-a')
        await sessions.receive_result(run)
        raise RuntimeError('backend failed after accepting the tool result')

    sessions = ToolSessions(events)
    target = BackendTarget('key:worker', 'ws://worker', '/workspace')
    assert [event async for event in sessions.stream(request(), target)][0].tool_call['call_id'] == 'call_failed'
    continued = ResponseRequest(model='test', input=[{'type': 'function_call_output', 'call_id': 'call_failed', 'output': 'answer'}])
    with pytest.raises(RuntimeError):
        _ = [event async for event in sessions.stream(continued, target)]
    with pytest.raises(ToolProtocolError) as error:
        sessions.target_for(continued, 'key')
    assert error.value.code == 'client_tool_call_unavailable'
    await sessions.close()


@pytest.mark.asyncio
async def test_expired_calls_fail_closed():
    sessions=None
    async def events(req,target,run):
        call={'call_id':'call_expired'}
        await sessions.await_result(run,call)
        yield BackendStreamEvent(tool_call=call,thread_id='thread-a',input_tokens=10,output_tokens=1,cache_read_tokens=6,cache_write_tokens=2)
        await sessions.receive_result(run)
    sessions=ToolSessions(events,ttl=0.01)
    target=BackendTarget('key:worker','ws://worker','/workspace')
    _=[e async for e in sessions.stream(request(),target)]
    await asyncio.sleep(0.03)
    r=ResponseRequest(model='test',input=[{'type':'function_call_output','call_id':'call_expired','output':'late'}])
    with pytest.raises(ToolProtocolError):sessions.target_for(r,'key')
    assert not sessions.runs
    await sessions.close()


@pytest.mark.asyncio
async def test_responses_sse_emits_structured_tools_after_persistence(monkeypatch):
    from codex_gateway import main
    from codex_gateway.auth import ApiPrincipal
    saved=[]
    async def save(*a,**kw):saved.append(a[6])
    monkeypatch.setattr(main,'save_usage',save)
    call=public_call(definitions(request()),{'tool':'gateway_client_0','arguments':{'query':'dns'}})
    class Backend:
        async def stream(self,*args):yield BackendStreamEvent(tool_call=call,thread_id='thread-a',input_tokens=10,output_tokens=1,cache_read_tokens=6,cache_write_tokens=2)
    stream=main.response_stream(request(),Backend(),ApiPrincipal(None,'test'),BackendTarget('key:worker','ws://worker','/workspace'),None)
    events=[]
    async for raw in stream:
        event=json.loads(raw.split('data: ',1)[1])
        if event['type']=='response.output_item.added':assert saved
        events.append(event)
    assert events[-1]['response']['output']==[call]
    assert any(e['type']=='response.function_call_arguments.done' for e in events)
    assert saved[0].tool_calls==[call]
    assert saved[0].cache_read_tokens==6 and saved[0].cache_write_tokens==2
    assert events[-1]["response"]["usage"]["input_tokens_details"]=={"cached_tokens":6,"cache_write_tokens":2}

@pytest.mark.asyncio
async def test_server_request_id_collision_does_not_consume_rpc_response():
    from codex_gateway.app_server import AppServerSession
    class Socket:
        def __init__(self):
            self.received=iter([
                {'id':1,'method':'item/tool/call','params':{'tool':'gateway_client_0'}},
                {'id':1,'result':{'turn':{'id':'turn-1'}}},
            ])
        async def send(self,raw):pass
        async def recv(self):return json.dumps(next(self.received))
    session=AppServerSession(Socket(),1)
    assert await session.call('turn/start',{}) == {'turn':{'id':'turn-1'}}
    assert session.pending_notifications[0]['method']=='item/tool/call'

@pytest.mark.asyncio
async def test_disconnected_stream_cancels_worker_before_tool_boundary():
    sessions=None
    cancelled=asyncio.Event()
    async def events(req,target,run):
        try:
            yield BackendStreamEvent(delta='hello',thread_id='thread-a')
            await asyncio.Future()
        finally:cancelled.set()
    sessions=ToolSessions(events)
    stream=sessions.stream(request(),BackendTarget('key:worker','ws://worker','/workspace'))
    assert (await anext(stream)).delta=='hello'
    await stream.aclose()
    assert cancelled.is_set() and not sessions.runs
    await sessions.close()

@pytest.mark.asyncio
async def test_chat_stream_preserves_four_metrics_and_tool_call(monkeypatch):
    from codex_gateway import main
    from codex_gateway.auth import ApiPrincipal
    from codex_gateway.schemas import ChatCompletionRequest
    saved=[]
    async def save(*args,**kwargs):saved.append(args[6])
    monkeypatch.setattr(main,'save_usage',save)
    call={'type':'function_call','call_id':'call_four_metrics','name':'lookup','arguments':'{}'}
    class Backend:
        async def stream(self,*args):
            yield BackendStreamEvent(tool_call=call,thread_id='thread-a',input_tokens=100,output_tokens=10,cache_read_tokens=60,cache_write_tokens=5)
    body=ChatCompletionRequest(model='test',messages=[{'role':'user','content':'lookup'}],stream=True,stream_options={'include_usage':True})
    events=[]
    async for raw in main.chat_completion_stream(body,body.to_response_request(),Backend(),ApiPrincipal(None,'test'),BackendTarget('key:worker','ws://worker','/workspace')):
        data=raw.split('data: ',1)[1].strip()
        if data!='[DONE]':events.append(json.loads(data))
    assert events[-1]['usage']['prompt_tokens_details']=={'cached_tokens':60,'cache_write_tokens':5}
    assert saved[0].cache_read_tokens==60 and saved[0].cache_write_tokens==5
    assert saved[0].tool_calls==[call]
    assert any(e['choices'] and e['choices'][0]['finish_reason']=='tool_calls' for e in events)
    response=main.chat_completion_object('id',0,body,saved[0])
    assert response['usage']==events[-1]['usage']
