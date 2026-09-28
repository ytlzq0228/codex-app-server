import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .admin_auth import (
    SESSION_COOKIE,
    AdminSession,
    require_admin,
    safe_next_url,
    verify_csrf,
)
from .app_server import AppServerError, open_app_server
from .backend import WorkerFailure, classify_worker_failure, run_healthcheck_turn
from .config import Settings, get_settings
from .database import get_session
from .history import conversation_history, active_conversation_groups, history_time_filters
from .models import GoogleAuthConfig, User, UserSession, ApiKey, ResponseBinding, SubscriptionPlan, UsageRecord, Worker, WorkerStatus
from .quota import quota_lock, ensure_capacity, reconcile_worker
from .subscriptions import DEFAULT_PLAN_COLOR, plan_pill_style
from .user_auth import issue_session, require_user, digest
from .security import generate_api_key, hash_api_key, hash_password, verify_password

auth_router = APIRouter(prefix="/auth", tags=["auth"])
user_router = APIRouter(prefix="/user", tags=["user-auth"])
router = APIRouter(prefix="/admin", tags=["admin"])
WORKER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")


def templates(request: Request):
    return request.app.state.templates


@auth_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "", session: AsyncSession = Depends(get_session)):
    google = await session.get(GoogleAuthConfig, 1)
    return templates(request).TemplateResponse(request, "login.html", {"next": safe_next_url(next), "error": None, "google_enabled": bool(google and google.enabled)})


@auth_router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    username = username.strip()
    # Full-email login is permitted only for the exact stored email, never by
    # dropping a supplied domain and authenticating an unrelated prefix owner.
    if "@" in username:
        matches = (await session.scalars(select(User).where(func.lower(User.email) == username.lower()))).all()
        user = matches[0] if len(matches) == 1 else None
    else:
        user = await session.get(User, username)
    google = await session.get(GoogleAuthConfig, 1)
    if not user or not user.enabled or not user.password_hash or not verify_password(password, user.password_hash):
        return templates(request).TemplateResponse(
            request,
            "login.html",
            {"next": safe_next_url(next), "error": "用户名或密码错误", "google_enabled": bool(google and google.enabled)},
            status_code=401,
        )
    destination = "/user/account" if user.must_change_password else ("/user/overview" if user.role == "user" else safe_next_url(next))
    return await issue_session(session, user, settings, RedirectResponse(destination, status_code=302))


@user_router.post("/account/password")
@router.post("/password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(min_length=12, max_length=256),
    confirm_password: str = Form(...),
    csrf_token: str = Form(...),
    admin: AdminSession = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    verify_csrf(request, admin, csrf_token)
    user = await session.get(User, admin.username)
    if not user or (user.password_hash and not verify_password(current_password, user.password_hash)):
        raise HTTPException(400, "当前密码不正确")
    if new_password != confirm_password:
        raise HTTPException(400, "两次输入的新密码不一致")
    if new_password == current_password:
        raise HTTPException(400, "新密码不能与当前密码相同")
    user.password_hash = hash_password(new_password)
    user.session_version += 1
    user.must_change_password = False
    return await issue_session(session, user, settings, result("密码已更新", "密码已修改，其他会话已失效。"))


@auth_router.post("/logout")
async def logout(
    request: Request,
    csrf_token: str = Form(...),
    admin: AdminSession = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    verify_csrf(request, admin, csrf_token)
    await session.execute(delete(UserSession).where(UserSession.token_hash == digest(request.cookies.get(SESSION_COOKIE, ""))))
    await session.commit()
    response = RedirectResponse("/auth/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def result(title: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"title": title, "message": message, **extra})


def manager_delete_succeeded(status_code: int) -> bool:
    """DELETE is idempotent: an already absent container is a successful outcome."""
    return status_code in {204, 404}


async def render_admin_page(request: Request, page: str, history_page: int, admin: AdminSession, session: AsyncSession, *, conversation: str = "", key_id: str = "", endpoint: str = "", start: str = "", end: str = ""):
    date_filters = history_time_filters(start, end)
    if page == "overview":
        stats = {
            "requests": await session.scalar(select(func.count()).select_from(UsageRecord)) or 0,
            "input_tokens": await session.scalar(select(func.coalesce(func.sum(UsageRecord.input_tokens), 0))) or 0,
            "output_tokens": await session.scalar(select(func.coalesce(func.sum(UsageRecord.output_tokens), 0))) or 0,
        }
        return templates(request).TemplateResponse(request, "admin/dashboard.html",
            {"page": page, "stats": stats, "csrf_token": admin.csrf_token})
    history_page = max(history_page, 1)
    history_page_size = 30
    keys = (await session.scalars(select(ApiKey).where(ApiKey.deleted_at.is_(None)).order_by(ApiKey.created_at.desc()))).all()
    workers = (await session.scalars(select(Worker).where(Worker.endpoint != "removed://worker").order_by(Worker.created_at.asc()))).all()
    plans = (await session.scalars(select(SubscriptionPlan))).all() if page == "admin_workers" else []
    plan_styles = {plan.name: plan_pill_style(plan.color) for plan in plans}
    history_keys = (await session.scalars(select(ApiKey).order_by(ApiKey.name, ApiKey.id))).all() if page == "history" else []
    history = await conversation_history(session, filters=date_filters, page=history_page, page_size=history_page_size, conversation_id=conversation, key_id=key_id, endpoint=endpoint)
    history_total = history["request_total"]
    history_session_total = history["total"]
    history_page, history_pages = history["page"], history["pages"]
    history_groups = history["groups"]
    binding_rows = (await session.execute(
        select(ResponseBinding, ApiKey, Worker, UsageRecord.logical_conversation_id, UsageRecord.thread_id, UsageRecord.endpoint)
        .join(ApiKey, ResponseBinding.api_key_id == ApiKey.id)
        .join(Worker, ResponseBinding.worker_id == Worker.id)
        .outerjoin(UsageRecord, (UsageRecord.request_id == ResponseBinding.response_id) & (UsageRecord.api_key_id == ResponseBinding.api_key_id))
        .where(ApiKey.deleted_at.is_(None), ResponseBinding.status == "active", Worker.endpoint != "removed://worker", ResponseBinding.worker_generation == Worker.execution_generation)
        .order_by(ResponseBinding.last_used_at.desc(), ResponseBinding.response_id.desc())
    )).all()
    active_sessions, sessions_by_key = active_conversation_groups(binding_rows)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    active_sessions = [group for group in active_sessions if group["latest_at"] >= cutoff]
    sessions_by_key = {}
    for group in active_sessions:
        sessions_by_key.setdefault(group["key"].id, []).append(group)
    if page == "sessions" and (conversation or key_id or endpoint):
        active_sessions = [group for group in active_sessions
            if (not conversation or group["conversation_id"] == conversation)
            and (not key_id or str(group["key"].id) == key_id)
            and (not endpoint or group["endpoint"] == endpoint)]
        sessions_by_key = {}
        for group in active_sessions:
            sessions_by_key.setdefault(group["key"].id, []).append(group)
    stats = {
        "requests": await session.scalar(select(func.count()).select_from(UsageRecord)) or 0,
        "input_tokens": await session.scalar(select(func.coalesce(func.sum(UsageRecord.input_tokens), 0))) or 0,
        "output_tokens": await session.scalar(select(func.coalesce(func.sum(UsageRecord.output_tokens), 0))) or 0,
    }
    return templates(request).TemplateResponse(
        request,
        "admin/dashboard.html",
        {"users": (await session.scalars(select(User).order_by(User.username))).all(), "page": page, "history_keys": history_keys, "keys": keys, "workers": workers, "plan_styles": plan_styles, "default_plan_style": plan_pill_style(DEFAULT_PLAN_COLOR), "history_groups": history_groups, "history_page": history_page, "history_pages": history_pages, "history_total": history_total, "history_session_total": history_session_total, "active_sessions": active_sessions, "sessions_by_key": sessions_by_key, "stats": stats, "csrf_token": admin.csrf_token, "show_cost": page == "history"},
    )


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return await render_admin_page(request, "overview", 1, admin, session)


@router.get("/monitoring")
async def overview_monitoring(days: int = 7, admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    if days not in (1, 7, 30, 90):
        raise HTTPException(400, "历史范围应为 1、7、30 或 90 天")
    from .monitoring import monitoring_data
    return JSONResponse(await monitoring_data(session, days), headers={"Cache-Control": "no-store"})


@router.get("/api-keys", response_class=HTMLResponse)
async def api_keys_page(request: Request, admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return await render_admin_page(request, "keys", 1, admin, session)


@router.get("/workers", response_class=HTMLResponse)
async def workers_admin_page(request: Request, admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return await render_admin_page(request, "admin_workers", 1, admin, session)


@router.get("/sessions", response_class=HTMLResponse)
async def sessions_page(request: Request, conversation: str = "", key_id: str = "", endpoint: str = "", admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return await render_admin_page(request, "sessions", 1, admin, session, conversation=conversation, key_id=key_id, endpoint=endpoint)


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request, start: str = "", end: str = "", history_page: int = 1, conversation: str = "", key_id: str = "", endpoint: str = "", admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return await render_admin_page(request, "history", history_page, admin, session, conversation=conversation, key_id=key_id, endpoint=endpoint, start=start, end=end)


@router.post("/keys")
async def create_key(
    request: Request,
    name: str = Form(min_length=1, max_length=120),
    scheduling_mode: str = Form("pooled"),
    pinned_worker_id: str = Form(""),
    csrf_token: str = Form(...),
    admin: AdminSession = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    verify_csrf(request, admin, csrf_token)
    pinned_id = await validate_key_schedule(session, scheduling_mode, pinned_worker_id)
    from .self_service import lock_available_owner
    await lock_available_owner(session, admin.username)
    raw_key, prefix = generate_api_key()
    settings = get_settings()
    record = ApiKey(owner_username=admin.username, name=name, prefix=prefix, key_hash=hash_api_key(raw_key, settings.key_pepper.get_secret_value()), scheduling_mode=scheduling_mode, pinned_worker_id=pinned_id)
    session.add(record)
    await session.commit()
    return result("API Key 已创建", "密钥只显示这一次，请立即复制并妥善保存。", secret=raw_key, key_id=str(record.id))


@router.post("/keys/{key_id}/toggle")
async def toggle_key(request: Request, key_id: UUID, csrf_token: str = Form(...), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    verify_csrf(request, admin, csrf_token)
    await quota_lock(session)
    record = await session.scalar(select(ApiKey).where(ApiKey.id == key_id).with_for_update().execution_options(populate_existing=True))
    if not record or record.deleted_at:
        raise HTTPException(404, "API key not found")
    if not record.enabled:
        await ensure_capacity(session, record.owner_username)
    record.enabled = not record.enabled
    await session.commit()
    return result("Key 状态已更新", f"{record.name} 已{'启用' if record.enabled else '停用'}。")


async def validate_key_schedule(session: AsyncSession, scheduling_mode: str, pinned_worker_id: str) -> UUID | None:
    if scheduling_mode not in {"pooled", "pinned"}:
        raise HTTPException(400, "无效的调度策略")
    try:
        pinned_id = UUID(pinned_worker_id) if pinned_worker_id else None
    except ValueError as exc:
        raise HTTPException(400, "无效的 Worker") from exc
    if scheduling_mode == "pinned":
        worker = await session.get(Worker, pinned_id) if pinned_id else None
        if not worker or not worker.enabled or worker.endpoint == "removed://worker":
            raise HTTPException(400, "固定调度必须选择一个可用 Worker")
        return pinned_id
    return None


@router.post("/keys/{key_id}/edit")
async def edit_key(
    request: Request,
    key_id: UUID,
    name: str = Form(min_length=1, max_length=120),
    scheduling_mode: str = Form(...),
    pinned_worker_id: str = Form(""),
    csrf_token: str = Form(...),
    admin: AdminSession = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    verify_csrf(request, admin, csrf_token)
    record = await session.get(ApiKey, key_id)
    if not record or record.deleted_at:
        raise HTTPException(404, "API key not found")
    record.name = name.strip()
    if not record.name:
        raise HTTPException(400, "Key 名称不能为空")
    record.scheduling_mode = scheduling_mode
    record.pinned_worker_id = await validate_key_schedule(session, scheduling_mode, pinned_worker_id)
    await session.commit()
    return result("Key 已更新", f"{record.name} 的名称和调度策略已保存。")


@router.post("/keys/{key_id}/delete")
async def delete_key(request: Request, key_id: UUID, csrf_token: str = Form(...), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    verify_csrf(request, admin, csrf_token)
    await quota_lock(session)
    record = await session.scalar(select(ApiKey).where(ApiKey.id == key_id).with_for_update())
    if not record or record.deleted_at:
        raise HTTPException(404, "API key not found")
    record.enabled = False
    record.deleted_at = datetime.now(timezone.utc)
    from .binding_lifecycle import invalidate_bindings
    await invalidate_bindings(session, api_key_id=key_id, reason="key_deleted")
    await session.commit()
    return result("Key 已删除", f"{record.name} 已失效，活动会话已释放；请求历史仍保留。")


@router.post("/keys/{key_id}/sessions/clear")
async def clear_key_sessions(request: Request, key_id: UUID, csrf_token: str = Form(...), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    verify_csrf(request, admin, csrf_token)
    record = await session.get(ApiKey, key_id)
    if not record or record.deleted_at:
        raise HTTPException(404, "API key not found")
    from .binding_lifecycle import invalidate_bindings
    deleted = await invalidate_bindings(session, api_key_id=key_id, reason="administrator_released")
    await session.commit()
    return result("活动会话已清空", f"{record.name} 的 {deleted} 条响应绑定已释放。")


@router.post("/sessions/{response_id}/delete")
async def delete_active_session(request: Request, response_id: str, csrf_token: str = Form(...), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    verify_csrf(request, admin, csrf_token)
    binding = await session.get(ResponseBinding, response_id)
    if not binding:
        raise HTTPException(404, "Active session not found")
    from .binding_lifecycle import invalidate_bindings
    deleted = await invalidate_bindings(session, api_key_id=binding.api_key_id, thread_id=binding.thread_id, reason="administrator_released")
    await session.commit()
    return result("Thread 绑定已释放", f"该 Thread 的 {deleted} 条响应绑定已释放。")


async def default_worker(session: AsyncSession, settings: Settings) -> Worker:
    worker = await session.scalar(select(Worker).where(Worker.name == "worker-1").with_for_update())
    if not worker:
        worker = Worker(owner_username=settings.admin_username, name="worker-1", container_name="codex-worker-1", endpoint=settings.app_server_url)
        session.add(worker)
        await session.flush()
    if worker.endpoint == "removed://worker":
        raise HTTPException(404, "默认 Worker 已删除")
    return worker


@router.post("/workers")
async def create_worker(request: Request, name: str = Form(min_length=1, max_length=48), provider: str = Form("codex"), csrf_token: str = Form(...), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)):
    verify_csrf(request, admin, csrf_token)
    if provider not in {"codex", "gemini"}:
        raise HTTPException(400, "不支持的 Worker 类型")
    name = name.strip().lower().replace("_", "-")
    if not WORKER_NAME_RE.fullmatch(name):
        raise HTTPException(400, "Worker 名称必须以小写字母开头，并且只能包含小写字母、数字和连字符")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{settings.manager_url}/workers", json={"name": name, "provider": provider}, headers={"Authorization": f"Bearer {settings.manager_token.get_secret_value()}"})
    if response.status_code >= 400:
        try:
            manager_message = response.json().get("detail", "Worker manager could not create the container")
        except ValueError:
            manager_message = "Worker manager could not create the container"
        status_code = response.status_code if 400 <= response.status_code < 500 else 502
        raise HTTPException(status_code, manager_message)
    data = response.json()
    session.add(Worker(provider=provider, owner_username=admin.username, name=name, container_name=data["name"], endpoint=data["endpoint"], status=WorkerStatus.offline))
    await session.commit()
    return result("Worker 已创建", f"{name} 的容器已经创建，可在列表中登录并探测状态。")


@router.post("/workers/{worker_id}/state")
async def toggle_worker_state(request: Request, worker_id: UUID, csrf_token: str = Form(...), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    verify_csrf(request, admin, csrf_token)
    worker = await session.scalar(select(Worker).where(Worker.id == worker_id).with_for_update().execution_options(populate_existing=True))
    if not worker or worker.endpoint == "removed://worker":
        raise HTTPException(404, "Worker not found")
    worker.status = WorkerStatus.draining if worker.status in {WorkerStatus.ready, WorkerStatus.busy} else WorkerStatus.ready
    worker.enabled = worker.status == WorkerStatus.ready
    if worker.status == WorkerStatus.ready:
        worker.account_checked_at = None
        worker.failure_kind = None
        worker.failure_reason = None
        worker.quarantined_at = None
        worker.retry_after = None
    await reconcile_worker(session, worker)
    await session.commit()
    return result("Worker 状态已更新", f"{worker.name} 当前状态：{worker.status.value}。")


@router.post("/workers/{worker_id}/delete")
async def delete_worker(request: Request, worker_id: UUID, csrf_token: str = Form(...), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)):
    verify_csrf(request, admin, csrf_token)
    worker = await session.scalar(select(Worker).where(Worker.id == worker_id).with_for_update().execution_options(populate_existing=True))
    if not worker or worker.endpoint == "removed://worker":
        raise HTTPException(404, "Worker not found")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.delete(f"{settings.manager_url}/workers/{worker.container_name}", headers={"Authorization": f"Bearer {settings.manager_token.get_secret_value()}"})
    if not manager_delete_succeeded(response.status_code):
        try:
            manager_message = response.json().get("detail", "Worker manager could not remove the container")
        except ValueError:
            manager_message = "Worker manager could not remove the container"
        raise HTTPException(502, manager_message)
    container_was_missing = response.status_code == 404
    from .binding_lifecycle import invalidate_bindings
    worker.execution_generation = (worker.execution_generation or 0) + 1
    await invalidate_bindings(session, worker_id=worker.id, reason="worker_deleted")
    worker.enabled = False
    worker.status = WorkerStatus.offline
    worker.endpoint = "removed://worker"
    await reconcile_worker(session, worker)
    await session.commit()
    message = (
        f"{worker.name} 的容器已经不存在；活动记录已清理，历史记录仍保留用于审计。"
        if container_was_missing
        else f"{worker.name} 的受管容器已删除，历史记录仍保留用于审计。"
    )
    return result("Worker 已删除", message)


async def probe_worker_record(worker: Worker, session: AsyncSession, settings: Settings) -> dict:
    if (worker.provider or "codex") == "gemini":
        from .gemini_backend import probe_gemini
        return await probe_gemini(worker, session, settings)
    from .contributions import update_account
    try:
        async with open_app_server(worker.endpoint, settings.app_server_token.get_secret_value(), settings.app_server_timeout_seconds) as app_server:
            response = await app_server.call("account/read", {"refreshToken": False})
            account = response.get("account") or {}
            if not account:
                raise WorkerFailure("Codex worker is not logged in", kind="logged_out", safe_to_retry=True)
            await update_account(worker, account)
            await run_healthcheck_turn(app_server, settings.upstream_model)
        was_error = worker.status == WorkerStatus.error
        worker.status = WorkerStatus.ready
        worker.auth_mode = account.get("type")
        worker.plan_type = account.get("planType")
        await update_account(worker, account)
        worker.last_seen_at = datetime.now(timezone.utc)
        worker.recovered_at = datetime.now(timezone.utc) if was_error else worker.recovered_at
        worker.failure_kind = None
        worker.failure_reason = None
        worker.quarantined_at = None
        worker.retry_after = None
        logged_in = True
        message = f"Worker 推理测试通过；账户类型：{worker.auth_mode}；套餐：{worker.plan_type or '—'}"
        ok = True
    except Exception as exc:
        worker.status = WorkerStatus.error
        message = f"Worker 探测失败：{exc}"
        kind = exc.kind if isinstance(exc, WorkerFailure) else classify_worker_failure(str(exc))
        if kind == "logged_out":
            await update_account(worker, None)
        worker.failure_kind = kind
        worker.failure_reason = str(exc)[:500]
        worker.quarantined_at = datetime.now(timezone.utc)
        cooldown = settings.worker_limit_cooldown_seconds if kind == "limit" else settings.worker_failure_cooldown_seconds
        worker.retry_after = datetime.now(timezone.utc) + timedelta(seconds=cooldown)
        ok = False
        logged_in = bool(worker.auth_mode)
    await reconcile_worker(session, worker)
    await session.commit()
    return {"title": "探测完成" if ok else "探测失败", "message": message, "ok": ok, "logged_in": logged_in, "auth_mode": worker.auth_mode, "plan_type": worker.plan_type}


@router.post("/workers/probe")
async def probe_worker(request: Request, csrf_token: str = Form(...), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)):
    verify_csrf(request, admin, csrf_token)
    worker = await default_worker(session, settings)
    return JSONResponse(await probe_worker_record(worker, session, settings))


@router.post("/workers/{worker_id}/probe")
async def probe_selected_worker(request: Request, worker_id: UUID, csrf_token: str = Form(...), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)):
    verify_csrf(request, admin, csrf_token)
    worker = await session.scalar(select(Worker).where(Worker.id == worker_id).with_for_update().execution_options(populate_existing=True))
    if not worker or worker.endpoint == "removed://worker":
        raise HTTPException(404, "Worker not found")
    return JSONResponse(await probe_worker_record(worker, session, settings))


async def login_worker_endpoint(endpoint: str, settings: Settings, *, force: bool = False, poll_url: str | None = None, on_logout=None) -> dict:
    try:
        async with open_app_server(endpoint, settings.app_server_token.get_secret_value(), settings.app_server_timeout_seconds) as app_server:
            account_response = await app_server.call("account/read", {"refreshToken": False})
            account = account_response.get("account") or {}
            if account and not force:
                return {"title": "Codex 已登录", "message": f"当前账户类型：{account.get('type') or 'chatgpt'}；套餐：{account.get('planType') or '—'}。无需重复登录。", "logged_in": True}
            if force:
                await app_server.call("account/logout", None)
                if on_logout:
                    await on_logout()
            response = await app_server.call("account/login/start", {"type": "chatgptDeviceCode"})
    except AppServerError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"title": "登录 Codex 账号", "message": "在 OpenAI 页面输入下方设备码。此窗口会自动检测登录结果。", "login_url": response.get("verificationUrl", ""), "user_code": response.get("userCode", ""), "logged_in": False, "poll_url": poll_url}


async def relogin_worker_record(worker, session, settings, *, force, poll_url):
    if (worker.provider or "codex") == "gemini":
        return {"message": "请在我的 Worker 页面使用 Gemini 登录窗口", "provider": "gemini"}
    logged_out = False

    async def record_logout():
        nonlocal logged_out
        from .contributions import update_account
        await update_account(worker, None, force_invalidate=True)
        worker.status = WorkerStatus.offline
        worker.failure_kind = "logged_out"
        await reconcile_worker(session, worker)
        logged_out = True

    try:
        return await login_worker_endpoint(worker.endpoint, settings, force=force,
            poll_url=poll_url, on_logout=record_logout)
    finally:
        # Logout is an external side effect: preserve it even if starting the
        # new login fails, retaining the worker row lock until the RPCs finish.
        if logged_out:
            await session.commit()


@router.post("/workers/login")
async def login_worker(request: Request, csrf_token: str = Form(...), force: bool = Form(False), admin: AdminSession = Depends(require_admin), settings: Settings = Depends(get_settings), session: AsyncSession = Depends(get_session)):
    verify_csrf(request, admin, csrf_token)
    worker = await default_worker(session, settings)
    return await login_selected_worker(request, worker.id, csrf_token, force, admin, session, settings)


@router.post("/workers/{worker_id}/login")
async def login_selected_worker(request: Request, worker_id: UUID, csrf_token: str = Form(...), force: bool = Form(False), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)):
    verify_csrf(request, admin, csrf_token)
    worker = await session.scalar(select(Worker).where(Worker.id == worker_id).with_for_update().execution_options(populate_existing=True))
    if not worker or worker.endpoint == "removed://worker":
        raise HTTPException(404, "Worker not found")
    payload = await relogin_worker_record(worker, session, settings, force=force,
        poll_url=f"/admin/workers/{worker.id}/probe")
    await session.commit()
    return JSONResponse(payload)
