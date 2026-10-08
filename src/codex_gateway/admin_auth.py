from .i18n import t
import hmac
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .database import get_session

SESSION_COOKIE = "codex_admin_session"
SESSION_MAX_AGE = 43_200


@dataclass(frozen=True)
class AdminSession:
    username: str
    csrf_token: str
    session_version: int = 0


def safe_next_url(value: str, default: str = "/admin") -> str:
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value and not any(ord(c) < 32 for c in value) else default


async def require_admin(request: Request, settings: Settings = Depends(get_settings), db: AsyncSession = Depends(get_session)) -> AdminSession:
    from .user_auth import require_user
    identity = await require_user(request, db)
    if request.state.user.role not in {"admin", "superadmin"}:
        raise HTTPException(403, t('需要管理员权限'))
    return identity


def verify_csrf(request: Request, session: AdminSession, supplied: str) -> None:
    if not supplied or not hmac.compare_digest(supplied, session.csrf_token):
        raise HTTPException(403, detail="Invalid security token")
