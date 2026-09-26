import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .database import get_session
from .models import AdminUser

SESSION_COOKIE = "codex_admin_session"
SESSION_MAX_AGE = 43_200


@dataclass(frozen=True)
class AdminSession:
    username: str
    csrf_token: str
    session_version: int = 0


def _secret(settings: Settings) -> bytes:
    configured = settings.admin_session_secret
    value = configured.get_secret_value() if configured else settings.admin_password.get_secret_value()
    return value.encode()


def create_admin_session(settings: Settings, username: str, session_version: int = 0) -> tuple[str, str]:
    csrf_token = secrets.token_urlsafe(24)
    payload = json.dumps(
        {"sub": username, "exp": int(time.time()) + SESSION_MAX_AGE, "csrf": csrf_token, "ver": session_version},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(_secret(settings), encoded, hashlib.sha256).digest()
    token = b".".join((encoded, base64.urlsafe_b64encode(signature).rstrip(b"="))).decode()
    return token, csrf_token


def decode_admin_session(token: str, settings: Settings) -> AdminSession | None:
    try:
        encoded, supplied_signature = token.split(".", 1)
        signature = base64.urlsafe_b64decode(supplied_signature + "=" * (-len(supplied_signature) % 4))
        expected = hmac.new(_secret(settings), encoded.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if payload.get("exp", 0) < int(time.time()):
            return None
        if not isinstance(payload.get("sub"), str) or not isinstance(payload.get("csrf"), str):
            return None
        return AdminSession(username=payload["sub"], csrf_token=payload["csrf"], session_version=int(payload.get("ver", 0)))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def safe_next_url(value: str, default: str = "/admin") -> str:
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value and not any(ord(c) < 32 for c in value) else default


async def require_admin(request: Request, settings: Settings = Depends(get_settings), db: AsyncSession = Depends(get_session)) -> AdminSession:
    from .user_auth import require_user
    identity = await require_user(request, db)
    if request.state.user.role not in {"admin", "superadmin"}:
        raise HTTPException(403, "需要管理员权限")
    return identity


def verify_csrf(request: Request, session: AdminSession, supplied: str) -> None:
    if not supplied or not hmac.compare_digest(supplied, session.csrf_token):
        raise HTTPException(403, detail="Invalid security token")
