import asyncio
from types import SimpleNamespace
import anyio
import pytest
from codex_gateway.conversations import explicit_identity, correlate, durable_write, digest
from codex_gateway.models import UsageRecord


def identity(thread='t', source='user', header=None, key='k'):
    params={'client_metadata': {'thread_id':thread,'x-codex-installation-id':'i',
        'x-codex-turn-metadata':'{"thread_source":"'+source+'"}'}}
    obs={'headers':[{'name':'originator','value':'codex-tui'}]}
    if header: obs['headers'].append({'name':'thread-id','value':header})
    return explicit_identity(params,obs,key,'responses','r')


def test_explicit_isolation_conflicts_and_title():
    assert identity()[0]==identity()[0]
    assert identity()[0]!=identity(key='other')[0]
    assert identity()[0]!=identity(source='thread_title')[0]
    assert identity(header='other')[0] is None
    assert identity(header='other')[1]['method']=='identifier_conflict'
    assert explicit_identity({'prompt_cache_key':'t'},None,'k','responses','r')[0] is None


@pytest.mark.asyncio
async def test_shadow_matches_actual_output_without_grouping():
    items=[{'role':'user','content':'hello'}]
    previous=UsageRecord(request_id='p',api_key_id='k',endpoint='chat.completions',status_code=200,
                         request_params={'model':'test','messages':items})
    class DB:
        async def scalars(self, query):
            return SimpleNamespace(all=lambda: [])
    await correlate(DB(),previous,'answer')
    expected=digest([{'model':'test','tools':None,'tool_choice':None},items+[{'role':'assistant','content':'answer'}]])
    assert previous.history_expected_hash==expected
    assert previous.logical_conversation_id is None
    assert previous.conversation_evidence['history_observation']['mode']=='shadow'


@pytest.mark.asyncio
async def test_durable_write_survives_cancelled_request_scope():
    saved=[]
    @durable_write
    async def save():
        await anyio.sleep(0)
        saved.append(True)
    with anyio.CancelScope() as scope:
        scope.cancel()
        await save()
    assert saved==[True]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['responses','chat'])
async def test_terminal_event_is_never_sent_before_persistence(monkeypatch,kind):
    from codex_gateway import main
    from codex_gateway.backend import MockBackend,BackendTarget
    from codex_gateway.auth import ApiPrincipal
    from codex_gateway.schemas import ResponseRequest,ChatCompletionRequest
    saved=[]
    async def save(*args,**kwargs):
        await asyncio.sleep(0)
        saved.append(args[6])
    monkeypatch.setattr(main,'save_usage',save)
    principal=ApiPrincipal(None,'test')
    target=BackendTarget('test','ws://test','/workspace')
    if kind=='responses':
        stream=main.response_stream(ResponseRequest(model='test',input='hello'),MockBackend(),principal,target,None)
    else:
        body=ChatCompletionRequest(model='test',messages=[{'role':'user','content':'hello'}])
        stream=main.chat_completion_stream(body,body.to_response_request(),MockBackend(),principal,target)
    async for event in stream:
        if '"type":"response.completed"' in event or '"finish_reason":"stop"' in event:
            assert len(saved)==1
            assert saved[0].text
            await stream.aclose()  # CLI closes immediately after terminal event.
            break
    else:
        pytest.fail('No terminal event')

@pytest.mark.asyncio
async def test_disconnect_keeps_authenticated_thread_and_survives_cancellation(monkeypatch):
    from codex_gateway import audit as module
    from codex_gateway import execution
    records = []
    class DB:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def add(self, record):
            records.append(record)
        async def commit(self):
            await anyio.sleep(0)
            records.append("committed")
    async def cleanup(audit):
        pass
    monkeypatch.setattr(module, "SessionLocal", DB)
    monkeypatch.setattr(execution, "cleanup", cleanup)
    async def app(scope, receive, send):
        await receive()
        audit = module.current_audit.get()
        audit["principal"] = SimpleNamespace(key_id="key", owner_username="owner")
        module.track_backend(SimpleNamespace(worker_id="worker"), "verified-thread",
                             source="authenticated_tool_call")
        await send({"type": "http.response.start", "status": 200})
        cancel.cancel()
    async def receive():
        return {"type": "http.request", "body": b'{"model":"gemini-test"}', "more_body": False}
    async def send(message):
        pass
    scope = {"type":"http", "method":"POST", "path":"/v1/chat/completions",
             "headers":[], "query_string":b"", "state":{}}
    with anyio.CancelScope() as cancel:
        await module.RequestAuditMiddleware(app)(scope, receive, send)
    assert records[-1] == "committed"
    record = records[0]
    assert record.status_code == 499
    assert record.thread_id == "verified-thread"
    assert record.worker_id == "worker"
    assert record.conversation_evidence["execution_outcome"] == "unknown"
    assert module.current_audit.get() is None
