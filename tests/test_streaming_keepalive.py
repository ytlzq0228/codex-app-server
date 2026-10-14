import asyncio
import json

import httpx
import pytest

from codex_gateway.streaming import keepalive
from codex_gateway.claude_native import ClaudeNativeMiddleware


@pytest.mark.asyncio
async def test_keepalive_does_not_cancel_or_restart_provider():
    ready, closed = asyncio.Event(), asyncio.Event()
    calls = []
    async def source():
        calls.append(1)
        try:
            await ready.wait()
            yield "data: result\n\n"
        finally:
            closed.set()
    stream = keepalive(source(), interval=.01)
    assert await anext(stream) == ": ping\n\n"
    assert await anext(stream) == ": ping\n\n"
    assert not closed.is_set()
    ready.set()
    assert await anext(stream) == "data: result\n\n"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert calls == [1] and closed.is_set()


@pytest.mark.asyncio
async def test_disconnect_closes_pending_provider():
    closed = asyncio.Event()
    async def source():
        try:
            await asyncio.Event().wait()
            yield ""
        finally:
            closed.set()
    stream = keepalive(source(), interval=.01)
    await anext(stream)
    await stream.aclose()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_native_messages_delivers_ping():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b": ping\n\n", "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ClaudeNativeMiddleware(inner)), base_url="http://test") as client:
        response = await client.post("/v1/messages", headers={"anthropic-version": "2023-06-01"},
            json={"model": "claude-test", "max_tokens": 10, "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    assert 'event: ping\ndata: {"type":"ping"}' in response.text
