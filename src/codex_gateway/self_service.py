import base64
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .admin_auth import AdminSession, require_admin, verify_csrf
from .config import get_settings
from .database import get_session
from .models import ApiKey, GoogleAuthConfig, OAuthState, User
from .security import generate_api_key, hash_api_key, hash_password
from .user_auth import digest, issue_session, require_user

router = APIRouter()


async def lock_available_owner(db, username, key_id=None):
    user = await db.scalar(select(User).where(User.username == username).with_for_update())
    if not user or not user.enabled:
        raise HTTPException(400, "请选择已存在且启用的用户")
    query = select(ApiKey.id).where(ApiKey.owner_username == username, ApiKey.deleted_at.is_(None))
    if key_id:
        query = query.where(ApiKey.id != key_id)
    if await db.scalar(query):
        raise HTTPException(409, "该用户已有 Key，请刷新现有 Key")
    return user


def render(request, identity, **context):
    return request.app.state.templates.TemplateResponse(request, "account.html", {
        "identity": request.state.user, "csrf_token": identity.csrf_token, **context})


@router.get("/account")
async def account(request: Request, identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    keys = (await db.scalars(select(ApiKey).where(ApiKey.owner_username == identity.username, ApiKey.deleted_at.is_(None)))).all()
    return render(request, identity, page="account", keys=keys)


@router.post("/account/key")
async def personal_key(request: Request, csrf_token: str = Form(...), identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    await lock_available_owner(db, identity.username)
    raw, prefix = generate_api_key()
    record = ApiKey(owner_username=identity.username, name=identity.username, prefix=prefix,
                    key_hash=hash_api_key(raw, get_settings().key_pepper.get_secret_value()))
    db.add(record)
    await db.commit()
    return {"secret": raw, "key_id": str(record.id), "message": "Key 仅显示一次，请妥善保存"}


@router.post("/account/keys/{key_id}/rotate")
async def rotate_key(request: Request, key_id: UUID, csrf_token: str = Form(...), identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    key = await db.scalar(select(ApiKey).where(ApiKey.id == key_id).with_for_update())
    if not key or key.deleted_at or (key.owner_username != identity.username and request.state.user.role not in {"admin", "superadmin"}):
        raise HTTPException(404, "Key 不存在")
    raw = f"cag_{key.prefix}_{secrets.token_urlsafe(32)}"
    key.key_hash = hash_api_key(raw, get_settings().key_pepper.get_secret_value())
    await db.commit()
    return {"secret": raw, "message": "Key 已刷新，旧值立即失效，ID 和前缀保持不变"}


@router.post("/admin/keys/{key_id}/owner")
async def transfer_key(request: Request, key_id: UUID, username: str = Form(...), csrf_token: str = Form(...), identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    await lock_available_owner(db, username, key_id)
    key = await db.scalar(select(ApiKey).where(ApiKey.id == key_id).with_for_update())
    if not key or key.deleted_at:
        raise HTTPException(404, "Key 不存在")
    key.owner_username = username
    await db.commit()
    return {"message": "Key 归属已更新"}


@router.get("/admin/users")
async def users(request: Request, identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    users = (await db.scalars(select(User).order_by(User.username))).all()
    keys = (await db.scalars(select(ApiKey).where(ApiKey.deleted_at.is_(None)))).all()
    return render(request, identity, page="users", users=users, keys=keys)


def authorize_role(actor, target_role, new_role):
    if new_role not in {"user", "admin", "superadmin"}:
        raise HTTPException(400, "角色无效")
    if actor.role != "superadmin" and (target_role != "user" or new_role != "user"):
        raise HTTPException(403, "只有 superadmin 可以管理管理员")


@router.post("/admin/users")
async def create_user(request: Request, username: str = Form(...), email: str = Form("", max_length=320), role: str = Form("user"), csrf_token: str = Form(...), identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    authorize_role(request.state.user, "user", role)
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,120}", username):
        raise HTTPException(400, "用户名仅支持字母、数字、点、下划线、@ 和连字符")
    password = secrets.token_urlsafe(18)
    db.add(User(username=username, email=email or None, role=role, password_hash=hash_password(password), must_change_password=True))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "用户名已存在")
    return {"secret": password, "message": "初始密码仅显示一次，用户下次登录必须修改密码"}


@router.post("/admin/users/{username}")
async def edit_user(request: Request, username: str, role: str = Form(...), email: str = Form("", max_length=320), enabled: bool = Form(False), reset_password: bool = Form(False), csrf_token: str = Form(...), identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    user = await db.scalar(select(User).where(User.username == username).with_for_update())
    if not user:
        raise HTTPException(404, "用户不存在")
    authorize_role(request.state.user, user.role, role)
    if username == identity.username and (role != user.role or not enabled or reset_password):
        raise HTTPException(400, "不能修改自己的角色、禁用自己或重置自己的密码")
    user.role, user.email, user.enabled = role, email or None, enabled
    user.session_version += 1
    password = None
    if reset_password:
        password = secrets.token_urlsafe(18)
        user.password_hash = hash_password(password)
        user.must_change_password = True
    await db.commit()
    return {"message": "用户已更新，旧会话已失效", "secret": password}


@router.get("/auth/google")
async def google_start(db: AsyncSession = Depends(get_session)):
    settings = get_settings()
    config = await google_config(db)
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    await db.execute(delete(OAuthState).where(OAuthState.expires_at <= datetime.now(timezone.utc)))
    db.add(OAuthState(state_hash=digest(state), verifier=verifier, expires_at=datetime.now(timezone.utc)+timedelta(minutes=10)))
    await db.commit()
    params = dict(client_id=config.client_id, redirect_uri=config.redirect_uri, response_type="code", scope="openid email profile", state=state, prompt="select_account", code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(), code_challenge_method="S256")
    response = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?"+urlencode(params), 302)
    response.set_cookie("google_state", state, httponly=True, secure=settings.admin_cookie_secure, samesite="lax", max_age=600)
    return response


@router.get("/auth/google/callback")
async def google_callback(request: Request, state: str = "", code: str = "", db: AsyncSession = Depends(get_session)):
    if not state or not secrets.compare_digest(state, request.cookies.get("google_state", "")):
        raise HTTPException(400, "Google 登录状态无效")
    stored = await db.scalar(delete(OAuthState).where(OAuthState.state_hash == digest(state)).returning(OAuthState))
    await db.commit()
    if not stored or stored.expires_at <= datetime.now(timezone.utc) or not code:
        raise HTTPException(400, "Google 登录已过期，请重试")
    config = await google_config(db)
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token = await client.post("https://oauth2.googleapis.com/token", data=dict(code=code, client_id=config.client_id, client_secret=config.client_secret, redirect_uri=config.redirect_uri, grant_type="authorization_code", code_verifier=stored.verifier))
            token.raise_for_status()
            info = await client.get("https://openidconnect.googleapis.com/v1/userinfo", headers={"Authorization": "Bearer " + token.json()["access_token"]})
            info.raise_for_status()
            profile = info.json()
    except (httpx.HTTPError, KeyError, ValueError):
        raise HTTPException(400, "Google 身份验证失败，请重试")
    email, sub = profile.get("email", "").lower(), profile.get("sub")
    domains = [x.strip().lower() for x in config.trusted_domains.split(",") if x.strip()]
    if not sub or profile.get("email_verified") is not True or "@" not in email or (domains and email.split("@")[-1] not in domains):
        raise HTTPException(403, "Google 邮箱未验证或不在允许的域中")
    user = await db.scalar(select(User).where(User.google_sub == sub))
    if not user:
        # Never implicitly link an existing local account based on email.
        username = email[:120]
        if await db.get(User, username):
            username = "google-" + secrets.token_hex(16)
        user = User(username=username, email=email, google_sub=sub, role="user", session_version=1, enabled=True)
        db.add(user)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            user = await db.scalar(select(User).where(User.google_sub == sub))
            if not user:
                raise HTTPException(409, "账号创建冲突，请重试")
    if not user.enabled:
        raise HTTPException(403, "账号已停用")
    response = RedirectResponse("/account", 302)
    response.delete_cookie("google_state")
    return await issue_session(db, user, settings, response)


async def google_config(db):
    config = await db.get(GoogleAuthConfig, 1)
    if not config or not config.enabled or not config.client_id or not config.client_secret or not config.redirect_uri:
        raise HTTPException(503, "Google SSO 尚未启用或配置不完整，请联系管理员")
    return config


@router.get("/admin/google")
async def google_settings(request: Request, identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    config = await db.get(GoogleAuthConfig, 1)
    return render(request, identity, page="google", config=config)


@router.post("/admin/google")
async def save_google_settings(request: Request, client_id: str = Form("", max_length=500), client_secret: str = Form("", max_length=1000), redirect_uri: str = Form("", max_length=1000), trusted_domains: str = Form("", max_length=2000), enabled: bool = Form(False), csrf_token: str = Form(...), identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    # Serialize initial creation as well as subsequent config updates.
    await db.scalar(select(User).where(User.username == get_settings().admin_username).with_for_update())
    config = await db.get(GoogleAuthConfig, 1)
    if not config:
        config = GoogleAuthConfig(id=1, client_secret="")
        db.add(config)
    uri = urlparse(redirect_uri.strip())
    if redirect_uri and (uri.scheme not in {"http", "https"} or not uri.netloc or uri.username or uri.password or uri.fragment or uri.query or uri.path != "/auth/google/callback"):
        raise HTTPException(400, "请输入完整回调地址，路径必须为 /auth/google/callback")
    if enabled and (not client_id.strip() or not (client_secret.strip() or config.client_secret) or not redirect_uri.strip()):
        raise HTTPException(400, "启用 Google 登录需要完整的 Client ID、Secret 和回调地址")
    domains = [domain.strip().lower().lstrip("@") for domain in trusted_domains.split(",") if domain.strip()]
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain) for domain in domains):
        raise HTTPException(400, "邮箱域格式无效，请使用逗号分隔")
    config.client_id, config.redirect_uri = client_id.strip(), redirect_uri.strip()
    config.enabled, config.trusted_domains = enabled, ",".join(domains)
    if client_secret.strip():
        config.client_secret = client_secret.strip()
    await db.commit()
    return {"message": "Google 登录配置已保存，立即生效"}
