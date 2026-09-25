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
    SESSION_MAX_AGE,
    AdminSession,
    create_admin_session,
    require_admin,
    safe_next_url,
    verify_csrf,
)
from .app_server import AppServerError, open_app_server
from .config import Settings, get_settings
from .database import get_session
from .models import AdminUser, ApiKey, ResponseBinding, UsageRecord, Worker, WorkerStatus
from .security import generate_api_key, hash_api_key, hash_password, verify_password

auth_router = APIRouter(tags=["admin-auth"])
router = APIRouter(prefix="/admin", tags=["admin"])
WORKER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")


def templates(request: Request):
    return request.app.state.templates


@auth_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "", session: AsyncSession = Depends(get_session)):
    if request.cookies.get(SESSION_COOKIE):
        from .admin_auth import decode_admin_session
        admin_session = decode_admin_session(request.cookies[SESSION_COOKIE], get_settings())
        user = await session.get(AdminUser, admin_session.username) if admin_session else None
        if user and user.session_version == admin_session.session_version:
            return RedirectResponse(safe_next_url(next), status_code=302)
    return templates(request).TemplateResponse(request, "login.html", {"next": safe_next_url(next), "error": None})


@auth_router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(AdminUser, username)
    if not user or not verify_password(password, user.password_hash):
        return templates(request).TemplateResponse(
            request,
            "login.html",
            {"next": safe_next_url(next), "error": "用户名或密码错误"},
            status_code=401,
        )
    token, _ = create_admin_session(settings, user.username, user.session_version)
    response = RedirectResponse(safe_next_url(next), status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.admin_cookie_secure,
        max_age=SESSION_MAX_AGE,
        path="/",
    )
    return response


@router.post("/password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(min_length=12, max_length=256),
    confirm_password: str = Form(...),
    csrf_token: str = Form(...),
    admin: AdminSession = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    verify_csrf(request, admin, csrf_token)
    user = await session.get(AdminUser, admin.username)
    if not user or not verify_password(current_password, user.password_hash):
        raise HTTPException(400, "当前密码不正确")
    if new_password != confirm_password:
        raise HTTPException(400, "两次输入的新密码不一致")
    if new_password == current_password:
        raise HTTPException(400, "新密码不能与当前密码相同")
    user.password_hash = hash_password(new_password)
    user.session_version += 1
    await session.commit()
    token, _ = create_admin_session(settings, user.username, user.session_version)
    response = result("密码已更新", "管理员密码已修改，其他已登录会话均已失效。")
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.admin_cookie_secure,
        max_age=SESSION_MAX_AGE,
        path="/",
    )
    return response


@auth_router.post("/logout")
async def logout(
    request: Request,
    csrf_token: str = Form(...),
    admin: AdminSession = Depends(require_admin),
):
    verify_csrf(request, admin, csrf_token)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def result(title: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"title": title, "message": message, **extra})


def manager_delete_succeeded(status_code: int) -> bool:
    """DELETE is idempotent: an already absent container is a successful outcome."""
    return status_code in {204, 404}


async def render_admin_page(request: Request, page: str, history_page: int, admin: AdminSession, session: AsyncSession):
    history_page = max(history_page, 1)
    history_page_size = 100
    keys = (await session.scalars(select(ApiKey).where(ApiKey.deleted_at.is_(None)).order_by(ApiKey.created_at.desc()))).all()
    workers = (await session.scalars(select(Worker).where(Worker.endpoint != "removed://worker").order_by(Worker.created_at.asc()))).all()
    history_total = await session.scalar(select(func.count()).select_from(UsageRecord)) or 0
    history_pages = max(1, (history_total + history_page_size - 1) // history_page_size)
    history_page = min(history_page, history_pages)
    history_rows = (await session.execute(
        select(UsageRecord, ApiKey.name, Worker.name)
        .outerjoin(ApiKey, UsageRecord.api_key_id == ApiKey.id)
        .outerjoin(Worker, UsageRecord.worker_id == Worker.id)
        .order_by(UsageRecord.created_at.desc())
        .offset((history_page - 1) * history_page_size)
        .limit(history_page_size)
    )).all()
    binding_rows = (await session.execute(
        select(ResponseBinding, ApiKey, Worker)
        .join(ApiKey, ResponseBinding.api_key_id == ApiKey.id)
        .join(Worker, ResponseBinding.worker_id == Worker.id)
        .where(ApiKey.deleted_at.is_(None))
        .order_by(ResponseBinding.created_at.desc())
    )).all()
    active_sessions = []
    sessions_by_key: dict[UUID, list] = {}
    seen_threads: dict[tuple[UUID, str], dict] = {}
    for binding, key, worker in binding_rows:
        identity = (key.id, binding.thread_id)
        if identity in seen_threads:
            seen_threads[identity]["binding_count"] += 1
            continue
        item = {"binding": binding, "key": key, "worker": worker, "binding_count": 1}
        seen_threads[identity] = item
        active_sessions.append(item)
        sessions_by_key.setdefault(key.id, []).append(item)
    stats = {
        "requests": await session.scalar(select(func.count()).select_from(UsageRecord)) or 0,
        "input_tokens": await session.scalar(select(func.coalesce(func.sum(UsageRecord.input_tokens), 0))) or 0,
        "output_tokens": await session.scalar(select(func.coalesce(func.sum(UsageRecord.output_tokens), 0))) or 0,
    }
    return templates(request).TemplateResponse(
        request,
        "admin/dashboard.html",
        {"page": page, "keys": keys, "workers": workers, "history_rows": history_rows, "history_page": history_page, "history_pages": history_pages, "history_total": history_total, "active_sessions": active_sessions, "sessions_by_key": sessions_by_key, "stats": stats, "csrf_token": admin.csrf_token},
    )


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return await render_admin_page(request, "overview", 1, admin, session)


@router.get("/api-keys", response_class=HTMLResponse)
async def api_keys_page(request: Request, admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return await render_admin_page(request, "keys", 1, admin, session)


@router.get("/sessions", response_class=HTMLResponse)
async def sessions_page(request: Request, admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return await render_admin_page(request, "sessions", 1, admin, session)


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request, history_page: int = 1, admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return await render_admin_page(request, "history", history_page, admin, session)


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
    if scheduling_mode not in {"pooled", "pinned"}:
        raise HTTPException(400, "Invalid scheduling mode")
    pinned_id = UUID(pinned_worker_id) if pinned_worker_id else None
    if scheduling_mode == "pinned" and not pinned_id:
        raise HTTPException(400, "固定调度必须选择 Worker")
    raw_key, prefix = generate_api_key()
    settings = get_settings()
    record = ApiKey(name=name, prefix=prefix, key_hash=hash_api_key(raw_key, settings.key_pepper.get_secret_value()), scheduling_mode=scheduling_mode, pinned_worker_id=pinned_id)
    session.add(record)
    await session.commit()
    return result("API Key 已创建", "密钥只显示这一次，请立即复制并妥善保存。", secret=raw_key, key_id=str(record.id))


@router.post("/keys/{key_id}/toggle")
async def toggle_key(request: Request, key_id: UUID, csrf_token: str = Form(...), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    verify_csrf(request, admin, csrf_token)
    record = await session.get(ApiKey, key_id)
    if not record or record.deleted_at:
        raise HTTPException(404, "API key not found")
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
    record = await session.get(ApiKey, key_id)
    if not record or record.deleted_at:
        raise HTTPException(404, "API key not found")
    record.enabled = False
    record.deleted_at = datetime.now(timezone.utc)
    await session.execute(delete(ResponseBinding).where(ResponseBinding.api_key_id == key_id))
    await session.commit()
    return result("Key 已删除", f"{record.name} 已失效，活动会话已释放；请求历史仍保留。")


@router.post("/keys/{key_id}/sessions/clear")
async def clear_key_sessions(request: Request, key_id: UUID, csrf_token: str = Form(...), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    verify_csrf(request, admin, csrf_token)
    record = await session.get(ApiKey, key_id)
    if not record or record.deleted_at:
        raise HTTPException(404, "API key not found")
    deleted = await session.execute(delete(ResponseBinding).where(ResponseBinding.api_key_id == key_id))
    await session.commit()
    return result("活动会话已清空", f"{record.name} 的 {deleted.rowcount or 0} 条响应绑定已释放。")


@router.post("/sessions/{response_id}/delete")
async def delete_active_session(request: Request, response_id: str, csrf_token: str = Form(...), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    verify_csrf(request, admin, csrf_token)
    binding = await session.get(ResponseBinding, response_id)
    if not binding:
        raise HTTPException(404, "Active session not found")
    deleted = await session.execute(delete(ResponseBinding).where(ResponseBinding.api_key_id == binding.api_key_id, ResponseBinding.thread_id == binding.thread_id))
    await session.commit()
    return result("活动会话已删除", f"该会话的 {deleted.rowcount or 0} 条响应绑定已释放。")


async def default_worker(session: AsyncSession, settings: Settings) -> Worker:
    worker = await session.scalar(select(Worker).where(Worker.name == "worker-1"))
    if not worker:
        worker = Worker(name="worker-1", container_name="codex-worker-1", endpoint=settings.app_server_url)
        session.add(worker)
        await session.flush()
    return worker


@router.post("/workers")
async def create_worker(request: Request, name: str = Form(min_length=1, max_length=48), csrf_token: str = Form(...), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)):
    verify_csrf(request, admin, csrf_token)
    name = name.strip().lower().replace("_", "-")
    if not WORKER_NAME_RE.fullmatch(name):
        raise HTTPException(400, "Worker 名称必须以小写字母开头，并且只能包含小写字母、数字和连字符")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{settings.manager_url}/workers", json={"name": name}, headers={"Authorization": f"Bearer {settings.manager_token.get_secret_value()}"})
    if response.status_code >= 400:
        try:
            manager_message = response.json().get("detail", "Worker manager could not create the container")
        except ValueError:
            manager_message = "Worker manager could not create the container"
        status_code = response.status_code if 400 <= response.status_code < 500 else 502
        raise HTTPException(status_code, manager_message)
    data = response.json()
    session.add(Worker(name=name, container_name=data["name"], endpoint=data["endpoint"], status=WorkerStatus.offline))
    await session.commit()
    return result("Worker 已创建", f"{name} 的容器已经创建，可在列表中登录并探测状态。")


@router.post("/workers/{worker_id}/state")
async def toggle_worker_state(request: Request, worker_id: UUID, csrf_token: str = Form(...), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    verify_csrf(request, admin, csrf_token)
    worker = await session.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    worker.status = WorkerStatus.draining if worker.status in {WorkerStatus.ready, WorkerStatus.busy} else WorkerStatus.ready
    worker.enabled = worker.status == WorkerStatus.ready
    if worker.status == WorkerStatus.ready:
        worker.failure_kind = None
        worker.failure_reason = None
        worker.quarantined_at = None
        worker.retry_after = None
    await session.commit()
    return result("Worker 状态已更新", f"{worker.name} 当前状态：{worker.status.value}。")


@router.post("/workers/{worker_id}/delete")
async def delete_worker(request: Request, worker_id: UUID, csrf_token: str = Form(...), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)):
    verify_csrf(request, admin, csrf_token)
    worker = await session.get(Worker, worker_id)
    if not worker or worker.name == "worker-1":
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
    worker.enabled = False
    worker.status = WorkerStatus.offline
    worker.endpoint = "removed://worker"
    await session.commit()
    message = (
        f"{worker.name} 的容器已经不存在；活动记录已清理，历史记录仍保留用于审计。"
        if container_was_missing
        else f"{worker.name} 的受管容器已删除，历史记录仍保留用于审计。"
    )
    return result("Worker 已删除", message)


async def probe_worker_record(worker: Worker, session: AsyncSession, settings: Settings) -> dict:
    try:
        async with open_app_server(worker.endpoint, settings.app_server_token.get_secret_value(), settings.app_server_timeout_seconds) as app_server:
            response = await app_server.call("account/read", {"refreshToken": False})
        account = response.get("account") or {}
        was_error = worker.status == WorkerStatus.error
        worker.status = WorkerStatus.ready
        worker.auth_mode = account.get("type")
        worker.plan_type = account.get("planType")
        worker.last_seen_at = datetime.now(timezone.utc)
        worker.recovered_at = datetime.now(timezone.utc) if was_error else worker.recovered_at
        worker.failure_kind = None
        worker.failure_reason = None
        worker.quarantined_at = None
        worker.retry_after = None
        logged_in = bool(account)
        if not logged_in:
            worker.status = WorkerStatus.error
            worker.failure_kind = "logged_out"
            worker.failure_reason = "Codex worker is not logged in"
            worker.quarantined_at = datetime.now(timezone.utc)
            worker.retry_after = datetime.now(timezone.utc) + timedelta(seconds=settings.worker_failure_cooldown_seconds)
        message = (
            f"Worker 可连接；账户类型：{worker.auth_mode}；套餐：{worker.plan_type or '—'}"
            if logged_in else "Worker 可连接，但 Codex 账号尚未登录。"
        )
        ok = True
    except Exception as exc:
        worker.status = WorkerStatus.error
        message = f"Worker 探测失败：{exc}"
        worker.failure_kind = "connection"
        worker.failure_reason = str(exc)[:500]
        worker.quarantined_at = datetime.now(timezone.utc)
        worker.retry_after = datetime.now(timezone.utc) + timedelta(seconds=settings.worker_failure_cooldown_seconds)
        ok = False
        logged_in = False
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
    worker = await session.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    return JSONResponse(await probe_worker_record(worker, session, settings))


async def login_worker_endpoint(endpoint: str, settings: Settings, *, force: bool = False, poll_url: str | None = None) -> dict:
    try:
        async with open_app_server(endpoint, settings.app_server_token.get_secret_value(), settings.app_server_timeout_seconds) as app_server:
            account_response = await app_server.call("account/read", {"refreshToken": False})
            account = account_response.get("account") or {}
            if account and not force:
                return {"title": "Codex 已登录", "message": f"当前账户类型：{account.get('type') or 'chatgpt'}；套餐：{account.get('planType') or '—'}。无需重复登录。", "logged_in": True}
            response = await app_server.call("account/login/start", {"type": "chatgptDeviceCode"})
    except AppServerError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"title": "登录 Codex 账号", "message": "在 OpenAI 页面输入下方设备码。此窗口会自动检测登录结果。", "login_url": response.get("verificationUrl", ""), "user_code": response.get("userCode", ""), "logged_in": False, "poll_url": poll_url}


@router.post("/workers/login")
async def login_worker(request: Request, csrf_token: str = Form(...), force: bool = Form(False), admin: AdminSession = Depends(require_admin), settings: Settings = Depends(get_settings)):
    verify_csrf(request, admin, csrf_token)
    return JSONResponse(await login_worker_endpoint(settings.app_server_url, settings, force=force, poll_url="/admin/workers/probe"))


@router.post("/workers/{worker_id}/login")
async def login_selected_worker(request: Request, worker_id: UUID, csrf_token: str = Form(...), force: bool = Form(False), admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)):
    verify_csrf(request, admin, csrf_token)
    worker = await session.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    return JSONResponse(await login_worker_endpoint(worker.endpoint, settings, force=force, poll_url=f"/admin/workers/{worker.id}/probe"))
