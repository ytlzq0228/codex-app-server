import base64
import json
import socket
from unittest.mock import Mock
import httpx
import pytest
from codex_gateway import gemini_images as images
from codex_gateway.gemini_native import translate_request
from codex_gateway.schemas import ResponseRequest, ChatCompletionRequest
from codex_gateway.backend import BackendTarget, WorkerFailure
from codex_gateway.gemini_backend import GeminiAdapter
from codex_gateway.config import get_settings
from codex_gateway.providers import validate_capabilities

PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='
URL = 'data:image/png;base64,' + PNG
PART = {'type': 'input_image', 'image_url': URL}

@pytest.mark.asyncio
async def test_order_and_continuation():
    request = ResponseRequest(model='gemini-test', input=[{'role':'user', 'content':[{'type':'input_text','text':'before'},PART,{'type':'input_text','text':'after'},PART]}])
    prompt, attachments = await images.prepare_images(request.worker_input())
    assert prompt.index('before') < prompt.index('image 1') < prompt.index('after') < prompt.index('image 2')
    assert attachments == [{'mimeType':'image/png','data':PNG}] * 2
    request._execution_input_items = [{'role':'user','content':[{'type':'input_text','text':'new'}, PART]}]
    _, attachments = await images.prepare_images(request.worker_input())
    assert len(attachments) == 1

@pytest.mark.asyncio
async def test_limits_and_mime(monkeypatch):
    with pytest.raises(ValueError, match='at most 8'):
        await images.prepare_images([{'type':'image','url':URL}] * 9)
    with pytest.raises(ValueError, match='MIME'):
        images.image_payload(b'<html>error</html>', 'image/png')
    monkeypatch.setattr(images, 'MAX_IMAGE_BYTES', 5)
    with pytest.raises(ValueError, match='10 MiB'):
        await images.prepare_images([{'type':'image','url':URL}])

@pytest.mark.parametrize('address', ['127.0.0.1','10.0.0.1','169.254.169.254','::1','::ffff:127.0.0.1','192.168.1.1'])
def test_private_urls_blocked(monkeypatch, address):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',(address,80))])
    with pytest.raises(ValueError, match='public IP'):
        images.download_image('http://example.test/image.png')

@pytest.mark.parametrize('url', ['file:///etc/passwd','http://u:p@example.com/a','https://example.com:8443/a'])
def test_unsafe_urls(url):
    with pytest.raises(ValueError):
        images.download_image(url)


def test_download_pins_public_address_and_blocks_redirect(monkeypatch):
    hosts=[]
    def resolve(host,*a,**k):
        hosts.append(host)
        return [(2,1,6,'',('93.184.216.34' if host=='public.test' else '127.0.0.1',80))]
    monkeypatch.setattr(socket,'getaddrinfo',resolve)
    connect=Mock();monkeypatch.setattr(socket,'create_connection',connect)
    response=Mock(status=302);response.getheader.return_value='http://private.test/secret'
    conn=Mock();conn.getresponse.return_value=response
    monkeypatch.setattr(images.http.client,'HTTPConnection',Mock(return_value=conn))
    with pytest.raises(ValueError, match='public IP'):
        images.download_image('http://public.test/a')
    assert connect.call_args.args[0] == ('93.184.216.34',80)
    assert hosts == ['public.test','private.test']
    conn.close.assert_called_once()


def test_native_inline_and_file_order():
    body={'contents':[{'role':'user','parts':[{'text':'before'},{'inlineData':{'mimeType':'image/png','data':PNG}},{'text':'after'}, {'fileData':{'mimeType':'image/jpeg','fileUri':'https://example.com/a.jpg'}}]}]}
    converted=ChatCompletionRequest(**translate_request(body,'gemini-test',False)).to_response_request()
    assert [p['type'] for p in converted.worker_input()] == ['text','text','image','text','image']
    assert converted.worker_input()[2]['url'] == URL

@pytest.mark.asyncio
@pytest.mark.parametrize('capability', [0,1])
async def test_adapter_sends_images_and_checks_old_workers(monkeypatch, capability):
    settings=get_settings();monkeypatch.setattr(settings,'model_providers','gemini-test:gemini')
    received=[]
    async def handle(request):
        if request.url.path == '/capabilities':
            return httpx.Response(200,json={'image_input':capability})
        received.append(json.loads(request.content))
        return httpx.Response(200,content=b'{"thread_id":"thread","delta":"red"}\n{"thread_id":"thread","done":true}\n')
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    request=ResponseRequest(model='gemini-test',input=[{'role':'user','content':[PART]}])
    validate_capabilities(request)
    adapter=GeminiAdapter(settings);target=BackendTarget('key','http://worker','/workspace/key',provider='gemini')
    if not capability:
        with pytest.raises(WorkerFailure,match='upgraded'):
            _=[event async for event in adapter.stream(request,target)]
        assert not received
    else:
        events=[event async for event in adapter.stream(request,target)]
        assert received[0]['images'] == [{'mimeType':'image/png','data':PNG}]
        assert 'gateway_read_image' in received[0]['prompt']
        assert events
