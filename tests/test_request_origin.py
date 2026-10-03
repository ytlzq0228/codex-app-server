"""Exercise the production request guard without database-backed routes."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_gateway.main import request_limits_and_headers


@pytest.mark.parametrize("path", ["/auth/login", "/user/account/key"])
@pytest.mark.parametrize("base_url", ["https://gateway.example.com", "http://192.0.2.24:8000"])
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
        ("request-origin", "same-origin", 200),
        ("request-origin", None, 200),
        (None, None, 200),
    ],
)
def test_form_origin_guard(path, base_url, origin, fetch_site, expected_status):
    app = FastAPI()
    app.middleware("http")(request_limits_and_headers)

    @app.post(path)
    async def form():
        return {"reached_route": True}

    headers = {}
    if origin == "request-origin":
        origin = base_url
    if origin is not None:
        headers["Origin"] = origin
    if fetch_site is not None:
        headers["Sec-Fetch-Site"] = fetch_site
    with TestClient(app, base_url=base_url) as client:
        # Even the GET that renders the form must retain same-origin metadata.
        assert client.get(path).headers["Referrer-Policy"] == "same-origin"
        response = client.post(path, headers=headers)
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert response.status_code == expected_status
    if expected_status == 403:
        assert response.json()["error"]["code"] == "cross_site_request"
    else:
        assert response.json() == {"reached_route": True}
