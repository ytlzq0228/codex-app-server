import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest

from codex_gateway.client_tools import definitions, dynamic_specs, public_call, ToolProtocolError
from codex_gateway.grammar_tools import _check, call_matches_grammar, validate_grammars
from codex_gateway.schemas import ResponseRequest

# Actual grammar shape used by Smartwork and Codex CLI (no user prompt/content).
EXEC_GRAMMAR = r'''
start: pragma_source | plain_source
pragma_source: PRAGMA_LINE NEWLINE SOURCE
plain_source: SOURCE
PRAGMA_LINE: /[ \t]*\/\/ @exec:[^\r\n]*/
NEWLINE: /\r?\n/
SOURCE: /[\s\S]+/
'''


def request(syntax='lark', source=EXEC_GRAMMAR):
    return ResponseRequest(model='test', input=[{'type':'additional_tools','tools':[
        {'type':'namespace','name':'functions','tools':[
            {'type':'custom','name':'exec','format':{'type':'grammar','syntax':syntax,'definition':source}}
        ]}]}, {'role':'user','content':'hello'}])


@pytest.mark.asyncio
async def test_client_exec_grammar_preserves_namespace_and_raw_input():
    req=request()
    await validate_grammars(req)
    specs=definitions(req)
    assert EXEC_GRAMMAR in dynamic_specs(specs)[0]['description']
    for text in ['text(await tools.echo("你好"));', '// @exec: {"yield_time_ms": 1000}\ntext("hello");']:
        params={'tool':'gateway_client_0','arguments':{'input':text}}
        call=public_call(specs,params)
        assert call['type']=='custom_tool_call' and call['namespace']=='functions'
        assert call['input']==text
        assert await call_matches_grammar(specs,params,call)
    assert not await _check(specs[0]['grammar'], '')


@pytest.mark.asyncio
async def test_regex_checks_entire_unicode_input_and_incomplete_text():
    grammar={'syntax':'regex','definition':r'你好-[0-9]{2}'}
    assert await _check(grammar,'你好-42')
    for text in ['你好-4','prefix你好-42','你好-42suffix','你好-42\n']:
        assert not await _check(grammar,text)


@pytest.mark.asyncio
@pytest.mark.parametrize('syntax,source',[
    ('lark','start: unknown_rule'),
    ('lark','%import ../../etc/passwd\nstart: PASSWORD'),
    ('lark','%import os.system\nstart: system'),
    ('regex','('),
])
async def test_invalid_grammars_fail_before_worker_use(syntax,source):
    with pytest.raises(ToolProtocolError):
        await validate_grammars(request(syntax,source))


@pytest.mark.parametrize('value',[
    {'type':'grammar','syntax':'python','definition':'print(1)'},
    {'type':'grammar','syntax':'lark','definition':''},
    {'type':'grammar','syntax':'lark','definition':'a'*32769},
    {'type':'unsupported'},
    'bad-format',
])
def test_unsupported_format_is_not_silently_ignored(value):
    req=ResponseRequest(model='test',input='hello',tools=[{'type':'custom','name':'exec','format':value}])
    assert req.unsupported()


@pytest.mark.asyncio
async def test_lark_builtin_import_and_grammar_text_never_execute(tmp_path):
    assert await _check({'syntax':'lark','definition':'start: INT\n%import common.INT'},'123')
    marker=tmp_path/'must-not-exist'
    text=f'__import__("pathlib").Path({str(marker)!r}).touch()'
    assert await _check({'syntax':'lark','definition':EXEC_GRAMMAR},text)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_parser_timeout_and_cancellation_kill_child(monkeypatch):
    from codex_gateway import grammar_tools
    entered=asyncio.Event()
    class Process:
        returncode=None
        killed=False
        async def communicate(self,data):
            entered.set()
            await asyncio.Future()
        def kill(self):self.killed=True;self.returncode=-9
        async def wait(self):return self.returncode
    processes=[]
    async def spawn(*a,**kw):
        proc=Process();processes.append(proc);return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr(grammar_tools,'TIMEOUT_SECONDS',0.01)
    with pytest.raises(ToolProtocolError,match='time limit'):
        await _check({'syntax':'regex','definition':'.*'})
    assert processes[0].killed
    entered.clear()
    task=asyncio.create_task(_check({'syntax':'regex','definition':'.*'}))
    await entered.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert processes[-1].killed


@pytest.mark.asyncio
@pytest.mark.parametrize('generated', [['bad','42'], ['bad','still bad','no'], ['bad']])
async def test_worker_only_exposes_valid_call_and_bounds_corrections(generated):
    from codex_gateway.backend import AppServerBackend, BackendTarget, WorkerFailure
    from codex_gateway.config import Settings
    backend=AppServerBackend(Settings())
    sent=[]; registered=[]
    async def send(raw):sent.append(json.loads(raw))
    class Server:
        websocket=SimpleNamespace(send=send)
        async def call(self,method,params):
            if method=='thread/start':
                assert params['environments']==[]
                assert not params['config']['features.shell_tool']
            return {'thread':{'id':'thread-a'}}
        async def messages(self):
            for i,text in enumerate(generated):
                yield {'id':f'rpc-{i}','method':'item/tool/call','params':{'tool':'gateway_client_0','arguments':{'input':text}}}
            yield {'method':'turn/completed','params':{'turn':{'status':'completed'}}}
    class Pool:
        @asynccontextmanager
        async def lease(self,*args):yield Server(),0
        async def invalidate(self,*args):pass
        async def close(self):pass
    backend.pool=Pool()
    async def register(run,call):registered.append(call)
    async def receive(run):return 'client-result'
    backend.tool_sessions.await_result=register
    backend.tool_sessions.receive_result=receive
    async def events():
        return [e async for e in backend._turn_events(request('regex','[0-9]{2}'),BackendTarget('key:worker','ws://worker','/workspace'),object())]
    try:
        if generated[-1]=='42':
            result=await events()
            calls=[e.tool_call for e in result if e.tool_call]
            assert [c['input'] for c in calls]==['42']
            assert len(registered)==1
            assert [r['result']['success'] for r in sent]==[False,True]
        else:
            with pytest.raises(WorkerFailure,match='grammar') as failure:await events()
            assert failure.value.kind=='request'
            assert not registered
            assert len(sent)<=2
    finally:await backend.close()

@pytest.mark.asyncio
async def test_invalid_grammar_generation_is_explicit_in_sse(monkeypatch):
    from codex_gateway import main
    from codex_gateway.auth import ApiPrincipal
    from codex_gateway.backend import BackendTarget, WorkerFailure
    saved=[]
    async def save(*args,**kwargs):saved.append(args[4])
    monkeypatch.setattr(main,'save_usage',save)
    class Backend:
        async def stream(self,*args):
            try:raise ToolProtocolError('Model input did not match grammar')
            except ToolProtocolError as exc:raise WorkerFailure(str(exc),kind='request') from exc
            yield
    raw=[e async for e in main.response_stream(request(),Backend(),ApiPrincipal(None,'test'),BackendTarget('key:worker','ws://worker','/workspace'),None)]
    final=json.loads(raw[-1].split('data: ',1)[1])
    assert final['type']=='response.failed'
    assert final['response']['error']=={'code':'invalid_client_tool','message':'Model input did not match grammar'}
    assert saved==[400]
    assert not any('response.output_item.added' in e for e in raw)
