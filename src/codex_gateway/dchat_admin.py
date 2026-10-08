"""Admin-only D-Chat configuration; secret values are never returned."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .admin_auth import require_admin, verify_csrf
from .config import get_settings
from .database import get_session
from .i18n import t
from .models import DChatConfig, User
from .page_data import data_page, page_response

router = APIRouter(prefix="/admin", tags=["admin"])


@data_page(router, "/dchat", "account.html", "dchat")
async def settings_page(request: Request, identity=Depends(require_admin),
                        db: AsyncSession = Depends(get_session)):
    config = await db.get(DChatConfig, 1)
    public = None if config is None else {
        "enabled": config.enabled, "bot_id": config.bot_id,
        "api_client_id": config.api_client_id,
        "has_secret": bool(config.api_client_secret),
    }
    return page_response(request, "account.html", {
        "page": "dchat", "identity": request.state.user,
        "csrf_token": identity.csrf_token, "config": public,
    })


@router.post("/dchat")
async def save_settings(request: Request,
    bot_id: str = Form("", max_length=120),
    api_client_id: str = Form("", max_length=500),
    api_client_secret: str = Form("", max_length=1000),
    enabled: bool = Form(False), csrf_token: str = Form(...),
    identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    # Serialize initial creation and credential updates just like Google SSO.
    await db.scalar(select(User).where(
        User.username == get_settings().admin_username).with_for_update())
    config = await db.get(DChatConfig, 1)
    secret = api_client_secret.strip() or (config.api_client_secret if config else "")
    if enabled and not (bot_id.strip() and api_client_id.strip() and secret):
        raise HTTPException(400, t("启用 D-Chat 需要完整的 Bot ID、Client ID 和 Secret"))
    if config is None:
        config = DChatConfig(id=1)
        db.add(config)
    config.enabled = enabled
    config.bot_id = bot_id.strip()
    config.api_client_id = api_client_id.strip()
    config.api_client_secret = secret
    await db.commit()
    return {"message": t("D-Chat 配置已保存，立即生效")}
