"""Exercise the production request guard without database-backed routes."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_gateway.main import request_limits_and_headers


@pytest.mark.parametrize("path", ["/auth/login", "/user/account/key"])
@pytest.mark.parametrize(
    "origin, fetch_site, expected_status",
    [
        ("null", "same-origin", 200),  # Headers from the failed login HAR.
        ("null", "same-site", 403),
        ("null", "cross-site", 403),
        ("null", "none", 403),
        ("null", None, 403),
        ("null", "unknown", 403),
        ("https://evil.test", "same-origin", 403),
        ("https://evil.test", "cross-site", 403),
        ("https://gateway.example.com", "same-origin", 200),
        ("https://gateway.example.com", None, 200),
        (None, None, 200),
    ],
)
def test_form_origin_guard(path, origin, fetch_site, expected_status):
    app = FastAPI()
    app.middleware("http")(request_limits_and_headers)

    @app.post(path)
    async def form():
        return {"reached_route": True}

    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if fetch_site is not None:
        headers["Sec-Fetch-Site"] = fetch_site
    with TestClient(app, base_url="https://gateway.example.com") as client:
        response = client.post(path, headers=headers)
    assert response.status_code == expected_status
    if expected_status == 403:
        assert response.json()["error"]["code"] == "cross_site_request"
    else:
        assert response.json() == {"reached_route": True}
