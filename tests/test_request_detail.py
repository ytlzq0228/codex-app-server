from datetime import datetime, timezone
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from codex_gateway.request_detail import last_texts, observation_fields, readable_fields


def test_last_texts_supports_responses_and_chat_without_tool_or_system_text():
    assert last_texts({'input': [
        {'role': 'user', 'content': [{'type': 'input_text', 'text': 'old'}]},
        {'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'answer'}]},
        {'role': 'user', 'content': [{'type': 'input_text', 'text': 'new'}, {'type': 'input_text', 'text': 'part two'}]},
        {'role': 'system', 'content': [{'type': 'input_text', 'text': 'hidden'}]},
        {'type': 'function_call_output', 'output': 'tool result'},
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
    assert fields['Client Address'] == '[::1]:1234'
    assert fields['User-Agent'] == 'test-client'
    assert fields['Client IP'] == '未记录'
    assert readable_fields({'method': 'explicit_client_thread', 'auto_resume': False,
                            'execution': {'action': 'resume'}}) == [
        ('关联方式', '客户端显式 Thread'), ('自动续用', '否'), ('执行决策 · 动作', '续用')]


def test_detail_template_escapes_text_and_collapses_raw_data_without_worker_ids():
    env = Environment(loader=FileSystemLoader('src/codex_gateway/templates'), autoescape=select_autoescape())
    record = SimpleNamespace(request_id='r1', model='test', status_code=200, endpoint='responses',
        duration_ms=1, created_at=datetime.now(timezone.utc), owner_username='alice',
        input_tokens=1, output_tokens=2, cache_read_tokens=0, cache_write_tokens=0,
        cost_usd=None, input_price=None, output_price=None, cache_read_price=None,
        cache_write_price=None, error_code=None, previous_response_id='prior',
        logical_conversation_id=None, thread_id=None, worker_id='private-worker-id')
    html = env.get_template('shared/request-detail.html').render(record=record,
        worker=SimpleNamespace(name='worker-a', account_email='a@example.com', owner_username='alice'),
        evidence_fields=[], observation_fields=[], last_texts={'input_text': '<script>alert(1)</script>'},
        correlation='{}', observation='null', params='{}')
    assert html.count('<details class="request-json">') == 3
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert 'private-worker-id' not in html
    assert all(value in html for value in ('worker-a', 'a@example.com', 'Previous Response', 'prior'))
    assert 'class="request-fields request-metadata-fields"' in html
    ordered_labels = ('输入 Token', '缓存读 Token', '缓存写 Token', '输出 Token',
                      '输入单价', '缓存读单价', '缓存写单价', '输出单价')
    positions = [html.index(label) for label in ordered_labels]
    assert positions == sorted(positions)
