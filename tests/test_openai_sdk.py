import httpx
import pytest
from openai import AsyncOpenAI

from codex_gateway.main import app


@pytest.mark.asyncio
async def test_official_openai_sdk_non_streaming_contract() -> None:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http_client:
            client = AsyncOpenAI(api_key="cag_dev_local", base_url="http://test/v1", http_client=http_client)
            models = await client.models.list()
            assert any(model.id == "codex" for model in models.data)

            response = await client.responses.create(model="codex", input="hello")
            assert response.id.startswith("resp_")
            assert response.output_text == "mock: hello"

            chat = await client.chat.completions.create(
                model="codex", messages=[{"role": "user", "content": "hello"}]
            )
            assert chat.choices[0].message.content == "mock: USER:\nhello"


@pytest.mark.asyncio
async def test_official_openai_sdk_streaming_contract() -> None:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http_client:
            client = AsyncOpenAI(api_key="cag_dev_local", base_url="http://test/v1", http_client=http_client)
            stream = await client.responses.create(model="codex", input="hello", stream=True)
            event_types = [event.type async for event in stream]
            assert event_types[0:2] == ["response.created", "response.in_progress"]
            assert event_types[-1] == "response.completed"

            chat_stream = await client.chat.completions.create(
                model="codex", messages=[{"role": "user", "content": "hello"}], stream=True
            )
            chunks = [chunk async for chunk in chat_stream]
            assert chunks[0].choices[0].delta.role == "assistant"
