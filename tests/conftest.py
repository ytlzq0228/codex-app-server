"""Mock backend tests should not require a running Docker worker manager."""
import os

# The development API key has no default any more; the suite opts into it before
# any Settings instance is built and cached.
os.environ.setdefault("CODEX_GATEWAY_DEV_API_KEY", "cag_dev_local")

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


@pytest.fixture(autouse=True)
def chinese_browser_default(monkeypatch):
    """Existing UI fixtures model a Chinese browser; language tests override it."""
    from starlette.testclient import TestClient
    original = TestClient.__init__

    def init(self, *args, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("accept-language", "zh-CN")
        return original(self, *args, headers=headers, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", init)
