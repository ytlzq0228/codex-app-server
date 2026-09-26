"""Mock backend tests should not require a running Docker worker manager."""
import httpx
import pytest
from codex_gateway.config import get_settings


@pytest.fixture(autouse=True)
def mock_workspace_manager(monkeypatch):
    original = httpx.AsyncClient.put

    async def put(client, url, **kwargs):
        settings = get_settings()
        if settings.backend == "mock" and str(url).startswith(settings.manager_url + "/workers/"):
            return httpx.Response(200, json={"ok": True})
        return await original(client, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "put", put)
