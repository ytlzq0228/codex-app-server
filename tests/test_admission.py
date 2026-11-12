from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from codex_gateway.main import app
from codex_gateway.database import SessionLocal
from codex_gateway.models import PendingUser, User, UserSession
from codex_gateway.admin_auth import SESSION_COOKIE
from test_self_service import admin_login, new_user, user_login, AJAX
from page_helpers import rendered_pages


def test_admission_lifecycle(monkeypatch):
    profile = {"sub": uuid4().hex, "email": "admission-" + uuid4().hex + "@example.com",
               "email_verified": True}
    original_post, original_get = httpx.AsyncClient.post, httpx.AsyncClient.get

    async def post(client, url, **kwargs):
        if url == "https://oauth2.googleapis.com/token":
            return httpx.Response(200, json={"access_token": "fixture"},
                                  request=httpx.Request("POST", url))
        return await original_post(client, url, **kwargs)

    async def get(client, url, **kwargs):
        if url == "https://openidconnect.googleapis.com/v1/userinfo":
            return httpx.Response(200, json=profile, request=httpx.Request("GET", url))
        return await original_get(client, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr(httpx.AsyncClient, "get", get)

    def login(client):
        start = client.get("/auth/google", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        return client.get("/auth/google/callback", params={"state": state, "code": "fixture"},
                          follow_redirects=False)

    async def check_pending(subject, username):
        async with SessionLocal() as db:
            assert await db.get(User, username) is None
            assert await db.get(PendingUser, subject) is not None
            assert await db.scalar(select(func.count()).select_from(UserSession).where(
                UserSession.username == username)) == 0

    async def check_approved(subject, username):
        async with SessionLocal() as db:
            assert await db.get(PendingUser, subject) is None
            user = await db.get(User, username)
            assert user.google_sub == subject and user.role == "user"
            assert user.enabled and user.password_hash is None

    with TestClient(app) as admin, nullcontext(TestClient(app)) as visitor:
        visitor.portal = admin.portal
        token = admin_login(admin)
        assert admin.post("/admin/google", data={
            "csrf_token": token, "enabled": "true", "client_id": "fixture",
            "client_secret": "fixture", "trusted_domains": "example.com",
            "redirect_uri": "http://testserver/auth/google/callback"}, headers=AJAX).status_code == 200

        def switch(enabled):
            response = admin.post("/admin/admissions/settings",
                                  data={"csrf_token": token, "enabled": str(enabled).lower()},
                                  headers=AJAX)
            assert response.status_code == 200, response.text

        switch(True)
        try:
            assert admin.post("/admin/admissions/settings", data={
                "csrf_token": "invalid"}, headers=AJAX).status_code == 403
            for _ in range(2):
                result = login(visitor)
                assert result.status_code == 403
                assert "等待管理员审批" in result.text
                assert SESSION_COOKIE not in visitor.cookies
            subject, username = profile["sub"], profile["email"].split("@")[0]
            admin.portal.call(check_pending, subject, username)
            assert visitor.get("/user/account/data", headers=AJAX).status_code == 401
            assert visitor.get("/admin/admissions/data", headers=AJAX).status_code == 401
            page = rendered_pages(admin, "/admin/admissions")
            assert page.status_code == 200 and username in page.text
            assert "审批通过" in page.text
            assert admin.post("/admin/admissions/approve", data={
                "csrf_token": "invalid", "subject": subject}, headers=AJAX).status_code == 403

            # Manual creation remains immediate, and normal users cannot review.
            manual, password = new_user(admin, token)
            with nullcontext(TestClient(app)) as local:
                local.portal = admin.portal
                local_token = user_login(local, manual, password)
                assert local.get("/admin/admissions/data", headers=AJAX).status_code == 403
                assert local.post("/admin/admissions/approve", data={
                    "csrf_token": local_token, "subject": subject}, headers=AJAX).status_code == 403

            switch(False)
            assert login(visitor).status_code == 403
            switch(True)
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda _: admin.post("/admin/admissions/approve",
                    data={"csrf_token": token, "subject": subject}, headers=AJAX), range(2)))
            assert sorted(r.status_code for r in responses) == [200, 404]
            admin.portal.call(check_approved, subject, username)
            assert login(visitor).status_code == 302
            assert visitor.get("/user/account/data").status_code == 200
            assert login(visitor).status_code == 302  # Existing users bypass review.

            # A different subject claiming the same username must never be linked.
            profile["sub"] = uuid4().hex
            assert login(visitor).status_code == 409

            # A manual account created after an application must not be overwritten.
            profile.update(sub=uuid4().hex, email="conflict-" + uuid4().hex + "@example.com")
            assert login(visitor).status_code == 403
            assert admin.post("/admin/users", data={"csrf_token": token,
                "username": profile["email"]}, headers=AJAX).status_code == 200
            assert admin.post("/admin/admissions/approve", data={"csrf_token": token,
                "subject": profile["sub"]}, headers=AJAX).status_code == 409

            switch(False)
            profile.update(sub=uuid4().hex, email="open-" + uuid4().hex + "@example.com")
            assert login(visitor).status_code == 302
        finally:
            switch(False)
