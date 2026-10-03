"""Smoke all page shells and JSON endpoints against an isolated mock database."""
import json
from pathlib import Path
from fastapi.testclient import TestClient
from codex_gateway.main import app
from codex_gateway.config import get_settings

paths = ["/admin", "/admin/api-keys", "/admin/workers", "/admin/sessions",
         "/admin/users", "/admin/finance", "/admin/reports", "/admin/google",
         "/user/overview", "/user/account", "/user/workers", "/user/usage", "/user/debug"]
def run():
    payloads = {}
    with TestClient(app) as client:
        settings = get_settings()
        response = client.post("/auth/login", data={"username": settings.admin_username,
            "password": settings.admin_password.get_secret_value()}, follow_redirects=False)
        assert response.status_code == 302, response.text
        for path in paths:
            shell = client.get(path)
            assert shell.status_code == 200, (path, shell.text)
            assert "data-json-page" in shell.text, path
            response = client.get(path + "/data")
            assert response.status_code == 200, (path, response.text)
            assert response.headers["cache-control"] == "no-store"
            data = response.json()
            assert "csrf_token" in data
            payloads[path] = data
            assert not any(x in response.text for x in ['"password_hash":', '"key_hash":', '"client_secret":', '"google_sub":'])
            print(path, len(response.content), "ok", flush=True)
    return payloads

if __name__ == "__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path)
    args=parser.parse_args()
    data=run()
    if args.fixtures:
        args.fixtures.write_text(json.dumps(data))
