import json

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosedError

from codex_gateway.app_server import connect_app_server
from codex_gateway.config import get_settings


@pytest.mark.asyncio
@pytest.mark.parametrize('compressed', [False, True])
async def test_large_rpc_and_notification_messages(compressed):
    payload = 'history' * 200_000

    async def worker(ws):
        request = json.loads(await ws.recv())
        await ws.send(json.dumps({'id': request['id'], 'result': {}}))
        await ws.recv()  # initialized
        request = json.loads(await ws.recv())
        await ws.send(json.dumps({'id': request['id'], 'result': {'history': payload}}))
        await ws.send(json.dumps({'method': 'item/completed', 'params': {'text': payload}}))
        await ws.wait_closed()

    async with serve(worker, '127.0.0.1', 0, compression='deflate' if compressed else None) as server:
        port = server.sockets[0].getsockname()[1]
        session = await connect_app_server(f'ws://127.0.0.1:{port}', 'test', timeout=5)
        try:
            assert (await session.call('thread/resume'))['history'] == payload
            messages = session.messages()
            assert (await anext(messages))['params']['text'] == payload
            await messages.aclose()
        finally:
            await session.close()


@pytest.mark.asyncio
async def test_configured_message_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(get_settings(), 'app_server_max_message_bytes', 1024)

    async def worker(ws):
        await ws.recv()
        await ws.send(json.dumps({'id': 1, 'result': {'text': 'x' * 2048}}))
        await ws.wait_closed()

    async with serve(worker, '127.0.0.1', 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(ConnectionClosedError) as exc:
            await connect_app_server(f'ws://127.0.0.1:{port}', 'test', timeout=5)
        assert exc.value.sent.code == 1009
