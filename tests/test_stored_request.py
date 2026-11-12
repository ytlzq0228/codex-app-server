import copy
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from codex_gateway.stored_request import stored_request_params


@pytest.mark.parametrize('text,expected', [
    ('', ''), ('a' * 64, 'a' * 64), ('a' * 65, 'a' * 64 + '...'),
    ('中' * 22, '中' * 21 + '...'), ('a' * 63 + '中', 'a' * 63 + '...'),
    ('😀' * 17, '😀' * 16 + '...'),
])
def test_utf8_limit(text, expected):
    original = {'input': [{'type': 'custom_tool_call_output', 'call_id': 'call-1', 'output': text}]}
    saved = stored_request_params(original)
    assert saved['input'][0] == {'type': 'custom_tool_call_output', 'call_id': 'call-1', 'output': expected}
    assert original['input'][0]['output'] == text


def test_protocols_and_non_tool_content():
    long = 'result ' * 100
    params = {
        'input': [
            {'type': kind, 'call_id': 'call-1', 'output': long}
            for kind in ('function_call_output', 'custom_tool_call_output', 'computer_call_output')
        ] + [{'role': 'user', 'content': long}, {'type': 'function_call', 'arguments': long}],
        'messages': [
            {'role': 'tool', 'tool_call_id': 'call-1', 'content': long},
            {'role': 'function', 'name': 'read', 'content': long},
            {'role': 'user', 'content': [{'type': 'text', 'text': long},
                                        {'type': 'tool_result', 'tool_use_id': 'call-2', 'content': long}]},
        ],
        'tools': [{'description': long}], 'instructions': long,
        'metadata': {'user_id': 'owner'},
    }
    original = copy.deepcopy(params)
    saved = stored_request_params(params)
    assert params == original
    for item in saved['input'][:3]:
        assert item['output'] == long[:64] + '...'
    assert saved['input'][3:] == params['input'][3:]
    assert saved['messages'][0]['content'] == long[:64] + '...'
    assert saved['messages'][0]['tool_call_id'] == 'call-1'
    assert saved['messages'][1]['content'] == long[:64] + '...'
    assert saved['messages'][2]['content'][0] == params['messages'][2]['content'][0]
    assert saved['messages'][2]['content'][1]['content'] == long[:64] + '...'
    assert saved['tools'] == params['tools']
    assert saved['instructions'] == long
    assert saved['metadata'] == params['metadata']


def test_structured_results_and_missing_fields():
    outputs = [None, [], {'ok': True}, [{'type': 'text', 'text': 'x' * 1000}]]
    params = {'input': [{'type': 'function_call_output', 'output': value} for value in outputs]}
    saved = stored_request_params(params)
    assert [item['output'] for item in saved['input'][:3]] == outputs[:3]
    assert len(saved['input'][3]['output'].encode()) == 67
    assert saved['input'][3]['output'].endswith('...')
    assert params['input'][3]['output'] == outputs[3]
    assert stored_request_params(None) is None
    assert stored_request_params({'input': 'user text'}) == {'input': 'user text'}
    assert stored_request_params({'input': [None, {'type': 'function_call_output'}]}) == {
        'input': [None, {'type': 'function_call_output'}]}


class DB:
    def __init__(self):
        self.records = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, *args):
        return None

    def add(self, record):
        self.records.append(record)

    async def commit(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 502])
@pytest.mark.parametrize('captured', [False, True])
async def test_usage_projects_after_correlation(monkeypatch, status, captured):
    from codex_gateway import main
    from codex_gateway.audit import current_audit
    from codex_gateway.auth import ApiPrincipal
    from codex_gateway.backend import BackendTarget
    from codex_gateway.schemas import BackendResult

    db = DB()
    monkeypatch.setattr(main, 'SessionLocal', lambda: db)
    original = {'model': 'test', 'input': [
        {'type': 'custom_tool_call_output', 'call_id': 'call-1', 'output': 'x' * 1000}]}
    before = copy.deepcopy(original)

    async def correlate(session, record, output):
        assert record.request_params == (before if captured else {**before, 'previous_response_id': None})
        record.history_expected_hash = 'full-history-hash'
        record.conversation_evidence = {'method': 'test'}

    monkeypatch.setattr(main, 'correlate', correlate)
    body = json.dumps(original).encode()
    audit = {'body': body, 'transport': {}, 'body_bytes_received': len(body),
             'body_hash': hashlib.sha256(body), 'body_complete': True} if captured else None
    token = current_audit.set(audit)
    try:
        await main.save_usage('resp-test', ApiPrincipal(None, 'test', owner_username='test'),
                              BackendTarget('test', 'ws://test', '/workspace'), 'test', status,
                              time.monotonic(), BackendResult(text='answer', thread_id='thread-test') if status == 200 else None,
                              request_params=original)
    finally:
        current_audit.reset(token)
    record = db.records[0]
    assert record.request_params['input'][0]['output'] == 'x' * 64 + '...'
    assert record.history_expected_hash == 'full-history-hash'
    assert record.owner_username == 'test'
    assert record.status_code == status
    assert record.response_text == ('answer' if status == 200 else None)
    assert original == before
    if captured:
        assert audit['body'] == body
        assert record.request_observation['body_sha256'] == hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize('complete', [False, True])
async def test_fallback_audit_preserves_forwarded_bytes(monkeypatch, complete):
    from codex_gateway import audit as module

    db = DB()
    monkeypatch.setattr(module, 'SessionLocal', lambda: db)
    monkeypatch.setattr(module, 'get_settings', lambda: SimpleNamespace(max_request_bytes=10000))
    original = {'model': 'test', 'input': [
        {'type': 'function_call_output', 'call_id': 'call-1', 'output': '中' * 100}]}
    body = json.dumps(original).encode()

    async def app(scope, receive, send):
        assert (await receive())['body'] == body
        audit = module.current_audit.get()
        audit['principal'] = SimpleNamespace(key_id=None, owner_username='test')
        assert module.request_params(audit) == original
        await send({'type': 'http.response.start', 'status': 400, 'headers': []})
        if complete:
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}

    async def send(message):
        pass

    scope = {'type': 'http', 'method': 'POST', 'path': '/v1/responses', 'headers': [],
             'query_string': b'', 'scheme': 'http', 'http_version': '1.1',
             'client': ('127.0.0.1', 1234), 'state': {}}
    await module.RequestAuditMiddleware(app)(scope, receive, send)
    record = db.records[0]
    assert record.request_params['input'][0]['output'] == '中' * 21 + '...'
    assert record.request_observation['body_sha256'] == hashlib.sha256(body).hexdigest()
    assert record.status_code == (400 if complete else 499)
