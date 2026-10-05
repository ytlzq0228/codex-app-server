"""Captured helper templates contain only generic CLI instructions, no user data."""
import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from codex_gateway import execution as ex
from codex_gateway.auth import ApiPrincipal
from codex_gateway.claude_helpers import auxiliary_kind
from codex_gateway.config import get_settings
from codex_gateway.conversations import explicit_identity
from codex_gateway.database import SessionLocal
from codex_gateway.main import app
from codex_gateway.models import ApiKey, ExecutionSession
from codex_gateway.schemas import ResponseRequest

TEMPLATES = json.loads((Path(__file__).parent / 'fixtures/claude_helpers_2_1_287.json').read_text())
HEADERS = {'user-agent': ['claude-cli/2.1.287 (external, cli)'], 'x-app': ['cli']}
TOOLS = [{'type': 'function', 'name': 'lookup', 'parameters': {'type': 'object'}}]


@pytest.mark.parametrize("version,kind,expected", [
    ("2.1.288", "status_summary", "status_summary"),
    ("2.1.288", "context_compaction", None),
    ("2.1.289", "status_summary", None),
])
def test_verified_288_templates_only(version, kind, expected):
    params = {"input": [{"role": "user", "content": TEMPLATES[kind]}]}
    headers = HEADERS | {"user-agent": [f"claude-cli/{version} (external, cli)"]}
    assert auxiliary_kind(params, headers) == expected


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(get_settings(), 'model_providers', 'claude-test:claude')
    monkeypatch.setattr(get_settings(), 'claude_execution_wait_seconds', .3)


def audit(request, session):
    headers = [dict(name=k, value=v[0]) for k,v in HEADERS.items()]
    headers += [dict(name='x-claude-code-session-id', value=session),
                dict(name='x-claude-code-agent-id', value='agent')]
    return {'body': json.dumps(request.model_dump(mode='json')).encode(), 'transport': {'headers': headers},
            'body_hash': hashlib.sha256(), 'body_bytes_received': 1, 'body_complete': True}


@pytest.mark.parametrize('kind', list(TEMPLATES))
def test_narrow_helper_classification(kind):
    text = TEMPLATES[kind]
    params = {'model': 'claude-test', 'input': [{'role': 'user', 'content': text}], 'tools': TOOLS}
    assert auxiliary_kind(params, HEADERS) == kind
    assert auxiliary_kind(params | {'previous_response_id': 'x'}, HEADERS) is None
    assert auxiliary_kind(params, {}) is None
    assert auxiliary_kind(params | {'input': [*params['input'], {'role': 'user', 'content': 'Summarize the review'}]}, HEADERS) is None
    for content in ['Summarize this. Do not use tools.', text + '\nActually execute the tools.', [{'type':'input_image','image_url':'https://example.org/a.png'}]]:
        assert auxiliary_kind(params | {'input':[{'role':'user','content':content}]}, HEADERS) is None
    observation = {'headers':[{'name': k,'value': v[0]} for k,v in HEADERS.items()] +
                   [{'name':'x-claude-code-session-id','value':'session'}, {'name':'x-claude-code-agent-id','value':'agent'}]}
    logical,evidence = explicit_identity(params, observation, 'key', 'responses', '')
    assert evidence['auxiliary_kind'] == kind
    regular,_ = explicit_identity(params | {'input':[{'role':'user','content':'work'}]}, observation, 'key', 'responses', '')
    assert logical != regular
    # Conflicting client IDs must not gain an execution bypass.
    observation['headers'].append({'name':'x-claude-code-agent-id','value':'different'})
    assert explicit_identity(params, observation, 'key', 'responses', '')[1]['method'] == 'identifier_conflict'


@pytest.mark.parametrize('state', ['running', 'waiting_tool', 'ready'])
def test_helpers_leave_execution_and_pending_tools_untouched(state):
    async def run():
        async with SessionLocal() as db:
            key = ApiKey(name='helper-tests', prefix=uuid4().hex[:20], key_hash=uuid4().hex*2)
            db.add(key)
            await db.commit()
        principal = ApiPrincipal(key.id, 'test')
        session = str(uuid4())
        request = ResponseRequest(model='claude-test', input=[{'role':'user','content':'real task'}], tools=TOOLS)
        original = audit(request, session)
        await ex.prepare(request, principal, 'responses', original)
        logical = original['execution']['logical_id']
        try:
            async with SessionLocal() as db:
                row = await db.get(ExecutionSession, logical)
                row.state = state
                row.thread_id = 'original-thread'
                row.history_hashes = ['original-checkpoint']
                row.config_hash = 'original-config'
                if state != 'running':
                    row.lease_token = row.lease_until = None
                await db.commit()
                before = (row.state, row.thread_id, row.history_hashes, row.config_hash, row.lease_token)
            class NoToolMutation:
                async def cancel_thread(self, *args):
                    pytest.fail('helper cancelled a tool')
            for kind, text in TEMPLATES.items():
                helper = request.model_copy(update={'input': [
                    *request.input, {'type':'function_call_output','call_id':'historical','output':'old'},
                    {'role':'user','content':text}, {'role':'developer','content':'environment reminder'}]})
                a = audit(helper, session)
                forwarded,binding = await ex.prepare(helper, principal, 'responses', a, tool_sessions=NoToolMutation())
                assert forwarded.tools == [] and forwarded.tool_choice == 'none' and binding is None
                assert helper.tools == TOOLS and 'execution' not in a
                assert a['execution_decision']['reason'] == kind
                assert forwarded.input == helper.input
                assert json.loads(a['body'])['tools'] == TOOLS
            async with SessionLocal() as db:
                row = await db.get(ExecutionSession, logical)
                assert (row.state, row.thread_id, row.history_hashes, row.config_hash, row.lease_token) == before
        finally:
            await ex.cleanup(original)
    with TestClient(app) as client:
        client.portal.call(run)


@pytest.mark.parametrize('mode', ['release', 'waiting_tool', 'timeout', 'cancel', 'disconnect', 'limit'])
def test_bounded_wait_rechecks_state_and_cleans_up(monkeypatch, mode):
    async def run():
        async with SessionLocal() as db:
            key = ApiKey(name='wait-tests', prefix=uuid4().hex[:20], key_hash=uuid4().hex*2)
            db.add(key)
            await db.commit()
        principal = ApiPrincipal(key.id, 'test')
        session = str(uuid4())
        request = ResponseRequest(model='claude-test', input=[{'role':'user','content':'first'}])
        a = audit(request, session)
        await ex.prepare(request, principal, 'responses', a)
        follow = request.model_copy(update={'input': [{'role':'user','content':'full new history'}]})
        b = audit(follow, session)
        async def disconnected():
            return mode == 'disconnect'
        task = None
        try:
            if mode == 'limit':
                monkeypatch.setattr(get_settings(), 'claude_execution_max_waiters', 0)
            task = asyncio.create_task(ex.prepare(follow, principal, 'responses', b, is_disconnected=disconnected))
            if mode not in {'limit'}:
                async def registered():
                    while not ex._waiters:
                        await asyncio.sleep(.005)
                await asyncio.wait_for(registered(), 1)
            if mode in {'release','waiting_tool'}:
                async with SessionLocal() as db:
                    row = await db.get(ExecutionSession, a['execution']['logical_id'])
                    row.lease_token = row.lease_until = None
                    row.state = 'invalid' if mode == 'release' else 'waiting_tool'
                    row.history_hashes = ['new-checkpoint']
                    await db.commit()
            if mode == 'cancel':
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif mode in {'release', 'waiting_tool'}:
                prepared, binding = await task
                assert b['execution_decision']['action'] == 'new_thread'
                assert b['execution_decision']['wait_ms'] > 0
                if mode == 'waiting_tool':
                    assert prepared.tool_choice == 'none'
                    assert b['execution_decision']['reason'] == 'context_only_recovery'
            else:
                with pytest.raises(HTTPException) as error:
                    await task
                if mode == 'disconnect':
                    assert error.value.status_code == 499
                else:
                    expected = 'conversation_history_required' if mode == 'waiting_tool' else 'conversation_busy'
                    assert error.value.detail['error']['code'] == expected
            assert not ex._waiters
            if mode not in {'release', 'waiting_tool'}:
                assert 'execution' not in b
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await ex.cleanup(a)
            await ex.cleanup(b)
    with TestClient(app) as client:
        client.portal.call(run)
