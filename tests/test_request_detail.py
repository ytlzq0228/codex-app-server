from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from jinja2 import Environment, FileSystemLoader, select_autoescape

from codex_gateway.display import money, tokens
from codex_gateway.request_detail import last_texts, observation_fields, readable_fields, request_texts


@pytest.mark.asyncio
async def test_actual_response_replaces_history_and_latest_input_is_used():
    record = SimpleNamespace(request_id='current', response_text='latest answer',
        previous_response_id=None, request_params={'messages': [
            {'role': 'user', 'content': 'old question'},
            {'role': 'assistant', 'content': 'old answer'},
            {'role': 'user', 'content': 'latest question'},
            {'role': 'assistant', 'content': 'intermediate answer'},
            {'role': 'tool', 'content': 'tool result'}]})
    assert await request_texts(AsyncMock(), record) == {
        'input_text': 'latest question', 'output_text': 'latest answer'}
    record.response_text = None
    assert await request_texts(AsyncMock(), record) == {'input_text': 'latest question'}
    record.response_text = ''
    assert '工具调用' in (await request_texts(AsyncMock(), record))['output_text']


@pytest.mark.asyncio
async def test_tool_continuation_follows_explicit_parent_for_input():
    record = SimpleNamespace(request_id='current', response_text='final',
        previous_response_id='parent', api_key_id=None, owner_username='alice',
        endpoint='responses', request_params={'input': [
            {'type': 'function_call_output', 'output': 'not a user message'}]})
    parent = SimpleNamespace(previous_response_id=None, request_params={'input': 'question'})
    db = AsyncMock()
    db.scalar.return_value = parent
    assert await request_texts(db, record) == {'input_text': 'question', 'output_text': 'final'}
    db.scalar.assert_awaited_once()


def test_last_texts_supports_responses_and_chat_without_tool_or_system_text():
    assert last_texts({'input': [
        {'role': 'user', 'content': [{'type': 'input_text', 'text': 'old'}]},
        {'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'answer'}]},
        {'role': 'user', 'content': [{'type': 'input_text', 'text': 'new'}, {'type': 'input_text', 'text': 'part two'}]},
        {'role': 'system', 'content': [{'type': 'input_text', 'text': 'hidden'}]},
        {'type': 'function_call_output', 'output': 'tool result'},
        {'role': 'tool', 'content': [{'type': 'input_text', 'text': 'not user input'}]},
    ]}) == {'input_text': 'new\n\npart two', 'output_text': 'answer'}
    assert last_texts({'messages': [{'role': 'user', 'content': 'question'},
                                    {'role': 'assistant', 'content': 'reply'}]}) == {
        'input_text': 'question', 'output_text': 'reply'}
    assert last_texts({'input': 'plain input'}) == {'input_text': 'plain input'}
    for value in (None, {}, {'input': None}, {'input': [None, {'content': None}]}):
        assert last_texts(value) == {}


def test_readable_observation_and_nested_evidence():
    fields = dict(observation_fields({'client_address': ['::1', 1234],
        'headers': [{'name': 'User-Agent', 'value': 'test-client'}], 'path': '/v1/responses'}))
    assert fields['客户端地址'] == '[::1]:1234'
    assert fields['User-Agent'] == 'test-client'
    assert fields['客户端 IP'] == '未记录'
    assert readable_fields({'method': 'explicit_client_thread', 'auto_resume': False,
                            'execution': {'action': 'resume'}}) == [
        ('关联方式', '客户端显式 Thread'), ('自动续用', '否'), ('执行决策 · 动作', '续用')]


def test_detail_template_escapes_text_and_collapses_raw_data_without_worker_ids():
    env = Environment(loader=FileSystemLoader('src/codex_gateway/templates'), autoescape=select_autoescape())
    env.filters.update(money=money, tokens=tokens)
    record = SimpleNamespace(request_id='r1', model='test', status_code=200, endpoint='responses',
        duration_ms=1, created_at=datetime.now(timezone.utc), owner_username='alice',
        input_tokens=1, output_tokens=2, cache_read_tokens=0, cache_write_tokens=0,
        cost_usd=None, input_price=None, output_price=None, cache_read_price=None,
        cache_write_price=None, error_code=None, previous_response_id='prior',
        logical_conversation_id=None, thread_id=None, worker_id='private-worker-id')
    from codex_gateway.i18n import t
    env.globals['t'] = t
    html = env.get_template('shared/request-detail.html').render(record=record,
        worker=SimpleNamespace(name='worker-a', account_email='a@example.com', owner_username='alice'),
        evidence_fields=[], observation_fields=[], last_texts={'input_text': '<script>alert(1)</script>'},
        correlation='{}', observation='null', params='{}', show_worker=True)
    assert html.count('<details class="request-json">') == 3
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert 'private-worker-id' not in html
    assert all(value in html for value in ('worker-a', 'a@example.com', 'Previous Response', 'prior'))
    assert 'class="request-fields request-metadata-fields"' in html
    hidden = env.get_template('shared/request-detail.html').render(record=record, worker=None,
        evidence_fields=[], observation_fields=[], last_texts={}, correlation='{}',
        observation='null', params='{}', show_worker=False)
    assert '<h3>Worker</h3>' not in hidden and 'a@example.com' not in hidden
    ordered_labels = ('输入 Token', '缓存读 Token', '缓存写 Token', '输出 Token',
                      '输入单价', '缓存读单价', '缓存写单价', '输出单价')
    positions = [html.index(label) for label in ordered_labels]
    assert positions == sorted(positions)
