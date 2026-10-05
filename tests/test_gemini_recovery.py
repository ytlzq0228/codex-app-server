from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from codex_gateway import execution as ex
from codex_gateway.auth import ApiPrincipal
from codex_gateway.config import get_settings
from codex_gateway.database import SessionLocal
from codex_gateway.main import app
from codex_gateway.models import ApiKey, ExecutionSession
from codex_gateway.schemas import ResponseRequest
from test_execution import audit_for


@pytest.mark.parametrize('case', [
    'changed_tools', 'reordered_history', 'busy', 'waiting', 'invalid',
    'delta_only', 'missing_output', 'orphan_output', 'duplicate_output', 'tool_tail',
    'duplicate_call', 'wrong_output_type', 'assistant_tail', 'reminder', 'recovered_history',
])
def test_gemini_completed_history_recovery(monkeypatch, case):
    monkeypatch.setattr(get_settings(), 'model_providers', 'gemini-test:gemini')

    async def run():
        async with SessionLocal() as db:
            key = ApiKey(name='gemini-recovery', prefix=uuid4().hex[:20], key_hash=uuid4().hex * 2)
            db.add(key)
            await db.commit()
        principal = ApiPrincipal(key.id, 'test')
        original = ResponseRequest(model='gemini-test', input=[{'role': 'user', 'content': 'inspect'}])
        call = {'type': 'function_call', 'call_id': 'done-call', 'name': 'inspect', 'arguments': '{}'}
        output = {'type': 'function_call_output', 'call_id': 'done-call', 'output': 'already executed'}
        answer = {'role': 'assistant', 'content': 'done'}
        session = str(uuid4())
        first = audit_for(original, session)
        await ex.prepare(original, principal, 'responses', first)
        await ex.cleanup(first)
        follow = original.model_copy(deep=True)
        follow.input = [*original.input, answer, call, output, {'role': 'user', 'content': 'next task'}]
        if case != 'reordered_history':
            follow.tools = [{'type': 'function', 'name': 'new_memory_tool', 'parameters': {'type': 'object'}}]
        if case == 'delta_only':
            follow.input = follow.input[-1:]
        if case in {'missing_output', 'recovered_history'}:
            follow.input.remove(output)
        if case == 'orphan_output':
            follow.input.remove(call)
        if case == 'duplicate_output':
            follow.input.insert(-1, output)
        if case == 'tool_tail':
            follow.input.pop()
        if case == 'duplicate_call':
            follow.input.insert(3, call)
        if case == 'wrong_output_type':
            follow.input[3] = {**output, 'type': 'custom_tool_call_output'}
        if case == 'assistant_tail':
            follow.input.append(answer)
        if case == 'reminder':
            follow.input.append({'role': 'developer', 'content': 'environment reminder'})
        async with SessionLocal() as db:
            row = await db.get(ExecutionSession, first['execution']['logical_id'])
            row.state = {'waiting': 'waiting_tool', 'invalid': 'invalid'}.get(case, 'ready')
            row.config_hash = ex.configuration(original)
            row.history_hashes = ex.hashes([*original.input, call, output, answer])
            if case == 'recovered_history':
                follow.input += [{'role': 'assistant', 'content': 'Recovered without tools'},
                                 {'role': 'user', 'content': 'Now use new_memory_tool'}]
                row.history_hashes = ex.hashes(follow.input[:-1])
            if case == 'busy':
                row.lease_token = str(uuid4())
                row.lease_until = ex.now() + timedelta(minutes=1)
            await db.commit()
        audit = audit_for(follow, session)
        try:
            if case in {'changed_tools', 'reordered_history', 'reminder', 'recovered_history'}:
                prepared, binding = await ex.prepare(follow, principal, 'responses', audit)
                assert binding is None and prepared.previous_response_id is None
                assert prepared.input == follow.input
                assert prepared.tools == follow.tools and prepared.tool_choice != 'none'
                assert not prepared._execution_auto_resume
                if case != 'recovered_history':
                    assert 'already executed' in prepared.input_text()
                assert audit['execution_decision']['action'] == 'new_thread'
                assert audit['execution_decision']['reason'] == (
                    'history_not_append_only' if case == 'reordered_history' else 'configuration_changed')
            elif case not in {"busy", "tool_tail", "assistant_tail"}:
                prepared, binding = await ex.prepare(follow, principal, "responses", audit)
                assert binding is None and prepared.previous_response_id is None
                assert prepared.tools == [] and prepared.tool_choice == "none"
                assert ex.RECOVERY_NOTICE in prepared.instructions
                assert prepared.input == follow.input
                assert audit["execution_decision"]["reason"] == "context_only_recovery"
            else:
                with pytest.raises(HTTPException) as error:
                    await ex.prepare(follow, principal, 'responses', audit)
                assert error.value.status_code == 409
                assert 'execution' not in audit
        finally:
            await ex.cleanup(audit)

    with TestClient(app) as client:
        client.portal.call(run)
