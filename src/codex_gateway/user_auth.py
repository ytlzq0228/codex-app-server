"""Database-backed browser sessions; only a digest of the cookie is stored."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from .admin_auth import AdminSession, SESSION_COOKIE, SESSION_MAX_AGE
from .database import get_session
from .models import User, UserSession


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def cookie_secure(settings, request):
    """Resolve the cookie Secure flag.

    Detection relies on uvicorn rewriting the scheme from X-Forwarded-Proto, which
    it only does for a proxy listed in --forwarded-allow-ips. Set the flag
    explicitly when the TLS terminator is not in that list.
    """
    if settings.admin_cookie_secure is not None:
        return settings.admin_cookie_secure
    return request.url.scheme == "https"


async def issue_session(db, user, settings, response, request):
    token = secrets.token_urlsafe(48)
    now = datetime.now(timezone.utc)
    await db.execute(delete(UserSession).where(UserSession.expires_at <= now))
    db.add(UserSession(token_hash=digest(token), username=user.username,
                       csrf_token=secrets.token_urlsafe(24), session_version=user.session_version,
                       expires_at=now + timedelta(seconds=SESSION_MAX_AGE)))
    await db.commit()
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=cookie_secure(settings, request), max_age=SESSION_MAX_AGE, path="/")
    return response


async def require_user(request: Request, db: AsyncSession = Depends(get_session)) -> AdminSession:
    stored = await db.get(UserSession, digest(request.cookies.get(SESSION_COOKIE, "")))
    user = await db.get(User, stored.username) if stored else None
    if not stored or not user or not user.enabled or stored.expires_at <= datetime.now(timezone.utc) or stored.session_version != user.session_version:
        if request.method != "GET" or request.headers.get("x-requested-with") == "XMLHttpRequest":
            raise HTTPException(401, "登录已过期")
        raise HTTPException(303, headers={"Location": "/auth/login?next=" + quote(request.url.path, safe="/")})
    if user.must_change_password and request.url.path not in {"/user/account", "/user/account/data", "/user/account/password", "/auth/logout", "/user/logout"}:
        raise HTTPException(403 if request.method != "GET" else 303, "请先修改初始密码", headers={"Location": "/user/account"})
    request.state.user = user
    return AdminSession(user.username, stored.csrf_token, stored.session_version)
