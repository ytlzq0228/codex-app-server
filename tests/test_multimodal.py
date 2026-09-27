import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from codex_gateway.backend import AppServerBackend, BackendTarget
from codex_gateway.client_tools import tool_outputs
from codex_gateway.config import Settings
from codex_gateway.execution import hashes, history_items, appended_items
from codex_gateway.multimodal import dynamic_output
from codex_gateway.schemas import ResponseRequest, ChatCompletionRequest

PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='
IMAGE = {'type': 'input_image', 'image_url': PNG, 'detail': 'original'}
PARTS = [{'type': 'text', 'text': 'before'}, IMAGE, {'type': 'output_text', 'text': 'after'}]
TOOL = {'type': 'function', 'name': 'view_image', 'parameters': {'type': 'object'}}


@pytest.mark.parametrize('kind', ['function_call_output', 'custom_tool_call_output'])
@pytest.mark.parametrize('output', [[IMAGE], PARTS, 'plain text'])
def test_tool_results_are_preserved(kind, output):
    req = ResponseRequest(model='test', input=[{'type': kind, 'call_id': 'call_a', 'output': output}])
    assert req.unsupported() is None
    call_id, parsed = tool_outputs(req)[0]
    assert call_id == 'call_a'
    actual = dynamic_output(parsed)
    if isinstance(output, str):
        assert actual == [{'type': 'inputText', 'text': output}]
    else:
        assert actual == [({'type': 'inputImage', 'imageUrl': PNG} if p['type'] == 'input_image'
                           else {'type': 'inputText', 'text': p['text']}) for p in output]


@pytest.mark.parametrize('part', [
    {'type': 'input_image'},
    {'type': 'input_image', 'file_id': 'file_123'},
    {'type': 'input_image', 'image_url': 'file:///etc/passwd'},
    {'type': 'input_image', 'image_url': '/tmp/screenshot.png'},
    {'type': 'input_image', 'image_url': 'data:image/png;base64,@@@'},
    {'type': 'input_image', 'image_url': 'data:text/plain;base64,YQ=='},
    {'type': 'input_image', 'image_url': 'https://'},
    {**IMAGE, 'detail': {}},
    {'type': 'input_audio', 'data': 'anything'},
    {'type': 'input_text', 'text': 123},
])
def test_invalid_parts_rejected_in_messages_and_all_tool_history(part):
    message = {'role': 'user', 'content': [part]}
    output = {'type': 'function_call_output', 'call_id': 'call_a', 'output': [part]}
    for items in [[message], [output], [output, {'role': 'user', 'content': 'next'}]]:
        assert ResponseRequest(model='test', input=items).unsupported() is not None


def test_responses_chat_and_replayed_tool_images_keep_order():
    req = ResponseRequest(model='test', instructions='inspect', input=[
        {'role': 'user', 'content': PARTS},
        {'type': 'function_call_output', 'call_id': 'old', 'output': [IMAGE]},
        {'role': 'user', 'content': 'compare'},
    ])
    assert req.unsupported() is None
    assert req.worker_input() == [
        {'type': 'text', 'text': 'inspect'}, {'type': 'text', 'text': 'USER:\n'},
        {'type': 'text', 'text': 'before'}, {'type': 'image', 'url': PNG, 'detail': 'original'},
        {'type': 'text', 'text': 'after'}, {'type': 'text', 'text': 'TOOL OUTPUT:\n'},
        {'type': 'image', 'url': PNG, 'detail': 'original'}, {'type': 'text', 'text': 'USER:\ncompare'},
    ]
    chat = ChatCompletionRequest(model='test', messages=[{'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': PNG, 'detail': 'high'}}]}])
    assert chat.unsupported() is None
    assert chat.to_response_request().worker_input()[-1] == {'type': 'image', 'url': PNG, 'detail': 'high'}
    chat_tool = ChatCompletionRequest(model='test', messages=[{
        'role': 'tool', 'tool_call_id': 'call_a', 'content': PARTS}])
    assert chat_tool.unsupported() is None
    assert dynamic_output(tool_outputs(chat_tool.to_response_request())[0][1])[1]['imageUrl'] == PNG
    url = 'https://example.org/image.png'
    assert ResponseRequest(model='test', input={'type': 'input_image', 'image_url': url}).worker_input() == [
        {'type': 'image', 'url': url}]


def test_image_history_fingerprints_and_private_delta():
    first = {'role': 'user', 'content': [IMAGE]}
    req = ResponseRequest(model='test', input=[first])
    expected = hashes(history_items(req))
    delta = {'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'https://example.org/new.png'}]}
    follow = ResponseRequest(model='test', input=[first, delta])
    assert appended_items(follow, expected) == [delta]
    changed = ResponseRequest(model='test', input=[delta, delta])
    assert appended_items(changed, expected) is None
    tool = {'type': 'function_call_output', 'call_id': 'old', 'output': [IMAGE]}
    assert hashes([tool]) != hashes([{**tool, 'output': delta['content']}])
    follow._execution_input_items = [delta]
    follow._execution_input_text = ''
    assert [p['url'] for p in follow.worker_input() if p['type'] == 'image'] == ['https://example.org/new.png']
    hostile = ResponseRequest(model='test', input=[first], _execution_input_items=[delta])
    assert hostile.worker_input()[-1]['url'] == PNG


@pytest.mark.asyncio
@pytest.mark.parametrize('custom', [False, True])
async def test_worker_roundtrip_tool_image_and_next_turn(custom):
    backend = AppServerBackend(Settings())
    calls, replies = [], []
    async def send(raw):
        replies.append(json.loads(raw))
    class Server:
        websocket = SimpleNamespace(send=send)
        async def call(self, method, params):
            calls.append((method, params))
            return {'thread': {'id': 'thread-a'}, 'turn': {'id': 'turn-a'}}
        async def messages(self):
            yield {'id': 7, 'method': 'item/tool/call', 'params': {
                'tool': 'gateway_client_0', 'arguments': {'input': 'view image'} if custom else {}}}
            assert replies[-1] == {'id': 7, 'result': {'success': True, 'contentItems': [
                {'type': 'inputText', 'text': 'before'}, {'type': 'inputImage', 'imageUrl': PNG},
                {'type': 'inputText', 'text': 'after'}]}}
            yield {'method': 'item/agentMessage/delta', 'params': {'delta': 'image received'}}
            yield {'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}
    class Pool:
        @asynccontextmanager
        async def lease(self, *args):
            yield Server(), 0
        async def close(self): pass
    backend.pool = Pool()
    target = BackendTarget('key:worker', 'ws://worker', '/workspace')
    req = ResponseRequest(model='test', input=[{'role': 'user', 'content': [IMAGE]}],
                          tools=[{'type': 'custom', 'name': 'view_image'} if custom else TOOL])
    try:
        first = [e async for e in backend._turn(req, target)]
        call = first[-1].tool_call
        continuation = ResponseRequest(model='test', input=[{
            'type': 'custom_tool_call_output' if custom else 'function_call_output',
            'call_id': call['call_id'], 'output': PARTS}])
        assert backend.continuation_target(continuation, 'key') == target
        result = [e async for e in backend._turn(continuation, target)]
        assert ''.join(e.delta for e in result) == 'image received'
        turns = [p for method, p in calls if method == 'turn/start']
        assert len(turns) == 1
        assert turns[0]['input'][-1] == {'type': 'image', 'url': PNG, 'detail': 'original'}
        assert not backend.tool_sessions.pending
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_missing_resume_replays_full_images():
    from codex_gateway.app_server import AppServerError
    class Server:
        async def call(self, method, params):
            if method == 'thread/resume':
                raise AppServerError('thread missing')
            return {'thread': {'id': 'new'}}
    backend = AppServerBackend(Settings())
    req = ResponseRequest(model='test', input=[{'role': 'user', 'content': [IMAGE]},
                                              {'role': 'user', 'content': 'next'}], previous_response_id='old')
    req._execution_input_items = [req.input[-1]]
    req._execution_input_text = 'USER:\nnext'
    req._execution_auto_resume = True
    try:
        assert req.worker_input() == [{'type': 'text', 'text': 'USER:\nnext'}]
        await backend._start_thread(Server(), req, '/workspace')
        assert any(p.get('url') == PNG for p in req.worker_input())
    finally:
        await backend.close()
