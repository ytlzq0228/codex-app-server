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
from .models import ApiKey, GoogleAuthConfig, OAuthState, User, Worker, WorkerStatus
from .security import generate_api_key, hash_api_key, hash_password
from .user_auth import cookie_secure, digest, issue_session, require_user

from .usernames import username_prefix

router = APIRouter()


async def lock_available_owner(db, username, key_id=None):
    from .quota import ensure_capacity
    return await ensure_capacity(db, username, key_id)


def render(request, identity, **context):
    return request.app.state.templates.TemplateResponse(request, "account.html", {
        "identity": request.state.user, "csrf_token": identity.csrf_token, **context})


@router.get("/user/overview")
async def overview(request: Request, identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    from .quota import quota_summary
    workers = (await db.scalars(select(Worker).where(Worker.owner_username == identity.username,
        Worker.endpoint != "removed://worker").order_by(Worker.created_at))).all()
    keys = (await db.scalars(select(ApiKey).where(ApiKey.owner_username == identity.username,
        ApiKey.deleted_at.is_(None)))).all()
    logged = [w for w in workers if w.auth_mode and w.account_checked_at and w.failure_kind != "logged_out"]
    attention = []
    for worker in workers:
        if worker not in logged:
            reason = "未登录或尚未确认登录，请登录后探测"
        elif not worker.enabled:
            reason = "已停用，请联系管理员确认"
        elif worker.failure_kind == "limit":
            reason = "账号已超限额，请等待额度恢复后探测"
        elif worker.status not in {WorkerStatus.ready, WorkerStatus.busy}:
            reason = "状态异常或离线，请探测并检查账号"
        else:
            continue
        attention.append({"name": worker.name, "reason": reason})
    healthy = bool(workers) and not attention
    completed = int(bool(logged)) + int(bool(keys)) + int(healthy)
    return render(request, identity, page="self_overview", workers=workers, logged_count=len(logged),
                  key_count=len(keys), enabled_key_count=sum(k.enabled for k in keys),
                  attention=attention, healthy=healthy, completed=completed,
                  quota=await quota_summary(db, identity.username))


@router.get("/user/account")
async def account(request: Request, identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    keys = (await db.scalars(select(ApiKey).where(ApiKey.owner_username == identity.username, ApiKey.deleted_at.is_(None)))).all()
    from .quota import quota_summary
    return render(request, identity, page="account", keys=keys, quota=await quota_summary(db, identity.username))


@router.post("/user/account/key")
async def personal_key(request: Request, name: str = Form("", max_length=120), csrf_token: str = Form(...), identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    await lock_available_owner(db, identity.username)
    raw, prefix = generate_api_key()
    record = ApiKey(owner_username=identity.username, name=name.strip() or identity.username, prefix=prefix,
                    key_hash=hash_api_key(raw, get_settings().key_pepper.get_secret_value()))
    db.add(record)
    await db.commit()
    return {"secret": raw, "key_id": str(record.id), "message": "Key 仅显示一次，请妥善保存"}


@router.post("/user/account/keys/{key_id}/rotate")
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
    from .quota import quota_lock, ensure_capacity
    await quota_lock(db)
    key = await db.scalar(select(ApiKey).where(ApiKey.id == key_id).with_for_update().execution_options(populate_existing=True))
    if not key or key.deleted_at:
        raise HTTPException(404, "Key 不存在")
    if key.enabled:
        await ensure_capacity(db, username, key_id)
    else:
        target = await db.get(User, username)
        if not target or not target.enabled:
            raise HTTPException(400, "请选择已存在且启用的用户")
    key.owner_username = username
    await db.commit()
    return {"message": "Key 归属已更新"}


@router.get("/admin/users")
async def users(request: Request, identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    users = (await db.scalars(select(User).order_by(User.username))).all()
    keys = (await db.scalars(select(ApiKey).where(ApiKey.deleted_at.is_(None)))).all()
    from .quota import quota_summary
    quotas = {user.username: await quota_summary(db, user.username) for user in users}
    return render(request, identity, page="users", users=users, keys=keys, quotas=quotas)


def authorize_role(actor, target_role, new_role):
    if new_role not in {"user", "admin", "superadmin"}:
        raise HTTPException(400, "角色无效")
    if actor.role != "superadmin" and (target_role != "user" or new_role != "user"):
        raise HTTPException(403, "只有 superadmin 可以管理管理员")


@router.post("/admin/users")
async def create_user(request: Request, username: str = Form(...), email: str = Form("", max_length=320), role: str = Form("user"), csrf_token: str = Form(...), identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    authorize_role(request.state.user, "user", role)
    original = username.strip()
    try:
        username = username_prefix(original)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if "@" in original and not email:
        email = original.lower()
    password = secrets.token_urlsafe(18)
    db.add(User(username=username, email=email or None, role=role, password_hash=hash_password(password), must_change_password=True))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "用户名已存在")
    return {"username": username, "secret": password, "message": f"用户 {username} 已创建，初始密码仅显示一次，用户下次登录必须修改密码"}


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


@router.post("/admin/users/{username}/providers")
async def edit_provider_grants(request: Request, username: str, codex: bool = Form(False),
                               gemini: bool = Form(False), csrf_token: str = Form(...),
                               identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    user = await db.scalar(select(User).where(User.username == username).with_for_update())
    if not user:
        raise HTTPException(404, "用户不存在")
    authorize_role(request.state.user, user.role, user.role)
    user.provider_grants = [name for name, enabled in (("codex", codex), ("gemini", gemini)) if enabled]
    await db.commit()
    return {"message": "Provider 额外授权已更新，对用户名下所有 Key 立即生效"}


@router.get("/auth/google")
async def google_start(request: Request, db: AsyncSession = Depends(get_session)):
    settings = get_settings()
    config = await google_config(db)
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    await db.execute(delete(OAuthState).where(OAuthState.expires_at <= datetime.now(timezone.utc)))
    db.add(OAuthState(state_hash=digest(state), verifier=verifier, expires_at=datetime.now(timezone.utc)+timedelta(minutes=10)))
    await db.commit()
    params = dict(client_id=config.client_id, redirect_uri=config.redirect_uri, response_type="code", scope="openid email profile", state=state, prompt="select_account", code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(), code_challenge_method="S256")
    response = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?"+urlencode(params), 302)
    response.set_cookie("google_state", state, httponly=True, secure=cookie_secure(settings, request), samesite="lax", max_age=600)
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
        try:
            username = username_prefix(email)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if await db.get(User, username):
            raise HTTPException(409, "邮箱前缀对应的用户名已存在，请联系管理员；不会自动合并账号")
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
    response = RedirectResponse("/user/account" if user.must_change_password else ("/user/overview" if user.role == "user" else "/admin"), 302)
    response.delete_cookie("google_state")
    return await issue_session(db, user, settings, response, request)


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
    if redirect_uri and (uri.scheme not in {"http", "https"} or not uri.netloc or uri.username or uri.password or uri.fragment or uri.query or uri.path not in {"/auth/google/callback", "/user/auth/google/callback"}):
        raise HTTPException(400, "请输入完整回调地址，路径应为 /auth/google/callback（兼容旧 /user/auth/google/callback）")
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


@router.post("/admin/users/{username}/quota")
async def grant_quota(request: Request, username: str, quota_granted: int | None = Form(None, ge=0, le=10000), amount: int | None = Form(None, ge=1, le=10000), csrf_token: str = Form(...), identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    from .quota import quota_lock, enforce_quota
    verify_csrf(request, identity, csrf_token)
    await quota_lock(db)
    user = await db.scalar(select(User).where(User.username == username).with_for_update().execution_options(populate_existing=True))
    if not user:
        raise HTTPException(404, "用户不存在")
    authorize_role(request.state.user, user.role, user.role)
    if quota_granted is None and amount is None:
        raise HTTPException(422, "请输入管理员授予额度")
    # Keep the old increment field for existing API clients; the UI sends an absolute value.
    target = quota_granted if quota_granted is not None else user.quota_granted + amount
    if target > 10000:
        raise HTTPException(422, "管理员授予额度不能超过 10000")
    user.quota_granted = target
    disabled = await enforce_quota(db, username)
    await db.commit()
    message = f"管理员授予额度已设为 {target}"
    if disabled:
        message += f"，额度不足，已停用 {disabled} 个最久未使用的 Key"
    return {"message": message}


@router.post("/user/account/keys/{key_id}/toggle")
async def personal_toggle(request: Request, key_id: UUID, csrf_token: str = Form(...), identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    from .quota import quota_lock, ensure_capacity
    verify_csrf(request, identity, csrf_token)
    await quota_lock(db)
    key = await db.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.owner_username == identity.username, ApiKey.deleted_at.is_(None)).with_for_update().execution_options(populate_existing=True))
    if not key:
        raise HTTPException(404, "Key 不存在")
    if not key.enabled:
        await ensure_capacity(db, identity.username)
    key.enabled = not key.enabled
    await db.commit()
    return {"message": "Key 已启用" if key.enabled else "Key 已停用，额度已释放"}


@router.post("/user/account/keys/{key_id}/delete")
async def personal_delete(request: Request, key_id: UUID, csrf_token: str = Form(...), identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    from .quota import quota_lock
    from .models import ResponseBinding
    verify_csrf(request, identity, csrf_token)
    await quota_lock(db)
    key = await db.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.owner_username == identity.username, ApiKey.deleted_at.is_(None)).with_for_update())
    if not key:
        raise HTTPException(404, "Key 不存在")
    key.enabled = False
    key.deleted_at = datetime.now(timezone.utc)
    from .binding_lifecycle import invalidate_bindings
    await invalidate_bindings(db, api_key_id=key_id, reason="key_deleted")
    await db.commit()
    return {"message": "Key 已删除，额度已释放，历史请求记录保留"}
