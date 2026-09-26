"""Owner-scoped worker management; Docker credentials never reach the browser."""
import re
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .admin_auth import require_admin, verify_csrf
from .app_server import open_app_server
from .backend import WorkerFailure, classify_worker_failure
from .config import get_settings
from .database import get_session
from .models import User, Worker, WorkerStatus
from .self_service import render
from .quota import reconcile_worker, credited_workers
from .user_auth import require_user

router = APIRouter()


def is_admin(request):
    return request.state.user.role in {"admin", "superadmin"}


async def owned_worker(request, db, worker_id, *, lock=True):
    query = select(Worker).where(Worker.id == worker_id, Worker.endpoint != "removed://worker")
    if not is_admin(request):
        query = query.where(Worker.owner_username == request.state.user.username)
    if lock:
        query = query.with_for_update()
    worker = await db.scalar(query.execution_options(populate_existing=True))
    if not worker:
        raise HTTPException(404, "Worker 不存在或无权访问")
    return worker


def update_account(worker, account):
    if account and worker.failure_kind == "logged_out":
        worker.failure_kind = None
    worker.auth_mode = account.get("type") if account else None
    worker.plan_type = account.get("planType") if account else None
    worker.account_email = account.get("email") if account else None
    worker.account_checked_at = datetime.now(timezone.utc)


def awaiting_login(worker):
    return not worker.auth_mode or not worker.account_checked_at or worker.failure_kind == "logged_out"


async def next_worker_suffix(db, username):
    prefix = username + "-worker-"
    names = await db.scalars(select(Worker.name).where(Worker.name.startswith(prefix, autoescape=True)))
    numbers = [int(name[len(prefix):]) for name in names if re.fullmatch(r"[0-9]{1,2}", name[len(prefix):])]
    number = max(numbers, default=0) + 1
    return f"{number:02d}" if number <= 99 else ""


@router.get("/workers")
async def workers_page(request: Request, identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    query = select(Worker).where(Worker.endpoint != "removed://worker")
    if not is_admin(request):
        query = query.where(Worker.owner_username == identity.username)
    workers = (await db.scalars(query.order_by(Worker.created_at))).all()
    users = (await db.scalars(select(User).where(User.enabled.is_(True)).order_by(User.username))).all() if is_admin(request) else []
    credited, duplicates = await credited_workers(db, identity.username if not is_admin(request) else None)
    blocked = not is_admin(request) and any(awaiting_login(worker) for worker in workers)
    suffix = await next_worker_suffix(db, identity.username) if not is_admin(request) else ""
    return render(request, identity, page="workers", workers=workers, users=users, credited=credited, duplicates=duplicates,
                  creation_blocked=blocked, next_suffix=suffix)


@router.post("/workers")
async def contribute_worker(request: Request, name: str = Form("", max_length=80), suffix: str = Form(""), csrf_token: str = Form(...), identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    # Names passed to Docker are server-generated, preventing name collisions or
    # access to another owner's retained Docker volumes.
    container_name = "contrib-" + uuid4().hex
    settings = get_settings()
    if is_admin(request):
        label = name.strip()
        if not label:
            raise HTTPException(400, "Worker 名称不能为空")
        label += "-" + container_name[-8:]
    else:
        # Serialize creation for this user, including the login check and naming.
        await db.scalar(select(User).where(User.username == identity.username).with_for_update())
        existing = (await db.scalars(select(Worker).where(
            Worker.owner_username == identity.username, Worker.endpoint != "removed://worker"))).all()
        if any(awaiting_login(worker) for worker in existing):
            raise HTTPException(409, "名下存在未登录或尚未确认登录的 Worker，请先登录并探测，或删除后再创建")
        if name:
            raise HTTPException(400, "名称前缀由当前用户名生成，只允许修改数字后缀")
        suffix = suffix or await next_worker_suffix(db, identity.username)
        if not re.fullmatch(r"[0-9]{1,2}", suffix) or not 1 <= int(suffix) <= 99:
            raise HTTPException(400, "请输入 01–99 的数字后缀，最多两位；序号用尽时请选择未使用的序号")
        label = f"{identity.username}-worker-{int(suffix):02d}"
        if await db.scalar(select(Worker.id).where(Worker.name == label)):
            raise HTTPException(409, "该 Worker 序号已经使用，请选择其他数字")
    worker = Worker(owner_username=identity.username, name=label,
                    container_name=container_name, endpoint=f"ws://{container_name}:4500", status=WorkerStatus.offline)
    db.add(worker)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "该 Worker 名称已经使用，请选择其他序号")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(settings.manager_url + "/workers", json={"name": container_name},
                headers={"Authorization": "Bearer " + settings.manager_token.get_secret_value()})
        if response.status_code >= 400:
            raise HTTPException(502, "Worker 创建失败，请重试")
        await db.commit()
    except httpx.HTTPError:
        await db.rollback()
        raise HTTPException(502, "Worker 管理服务暂时不可用")
    return {"message": "Worker 已创建，请登录账号后探测状态", "worker_id": str(worker.id)}


@router.post("/workers/{worker_id}/login")
async def contributor_login(request: Request, worker_id: UUID, force: bool = Form(False), csrf_token: str = Form(...), identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    from .admin import relogin_worker_record
    verify_csrf(request, identity, csrf_token)
    worker = await owned_worker(request, db, worker_id)
    result = await relogin_worker_record(worker, db, get_settings(), force=force,
        poll_url=f"/workers/{worker.id}/probe")
    await db.commit()
    return result


@router.post("/workers/{worker_id}/probe")
async def contributor_probe(request: Request, worker_id: UUID, csrf_token: str = Form(...), identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    from .admin import probe_worker_record
    verify_csrf(request, identity, csrf_token)
    worker = await owned_worker(request, db, worker_id)
    return await probe_worker_record(worker, db, get_settings())


@router.post("/workers/{worker_id}/account")
async def contributor_account(request: Request, worker_id: UUID, csrf_token: str = Form(...), identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    worker = await owned_worker(request, db, worker_id)
    settings = get_settings()
    try:
        async with open_app_server(worker.endpoint, settings.app_server_token.get_secret_value(), 20) as server:
            account = (await server.call("account/read", {"refreshToken": True})).get("account")
        update_account(worker, account)
        if not account:
            worker.status = WorkerStatus.error
            worker.failure_kind = "logged_out"
        await reconcile_worker(db, worker)
        await db.commit()
    except Exception as exc:
        worker.status = WorkerStatus.error
        worker.failure_kind = exc.kind if isinstance(exc, WorkerFailure) else classify_worker_failure(str(exc))
        if worker.failure_kind == "logged_out":
            update_account(worker, None)
        await reconcile_worker(db, worker)
        await db.commit()
        raise HTTPException(502, "无法读取 Worker 账号，贡献额度已撤销，请探测状态后重试")
    return {"message": "账号信息已更新", "account": {"email": worker.account_email, "type": worker.auth_mode, "plan": worker.plan_type}, "logged_in": bool(account)}


@router.post("/workers/{worker_id}/delete")
async def contributor_delete(request: Request, worker_id: UUID, csrf_token: str = Form(...), identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    from .admin import delete_worker
    verify_csrf(request, identity, csrf_token)
    await owned_worker(request, db, worker_id)
    return await delete_worker(request, worker_id, csrf_token, identity, db, get_settings())


@router.post("/admin/workers/{worker_id}/owner")
async def transfer_worker(request: Request, worker_id: UUID, username: str = Form(...), csrf_token: str = Form(...), identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    worker = await owned_worker(request, db, worker_id)
    user = await db.get(User, username)
    if not user or not user.enabled:
        raise HTTPException(400, "请选择已有且启用的用户")
    old_owner = worker.owner_username
    worker.owner_username = username
    await reconcile_worker(db, worker, old_owner)
    await db.commit()
    return {"message": "Worker 归属已更新"}


async def refresh_worker_account(worker_id):
    from datetime import timedelta
    from .database import SessionLocal
    from .admin import probe_worker_record
    settings = get_settings()
    async with SessionLocal() as db:
        worker = await db.scalar(select(Worker).where(Worker.id == worker_id, Worker.enabled.is_(True), Worker.endpoint != "removed://worker").with_for_update(skip_locked=True))
        if not worker:
            return
        if worker.status in {WorkerStatus.offline, WorkerStatus.error}:
            if not worker.retry_after or worker.retry_after <= datetime.now(timezone.utc):
                await probe_worker_record(worker, db, settings)
            return
        try:
            async with open_app_server(worker.endpoint, settings.app_server_token.get_secret_value(), 20) as server:
                account = (await server.call("account/read", {"refreshToken": True})).get("account")
            update_account(worker, account)
            if not account:
                worker.status = WorkerStatus.error
                worker.failure_kind = "logged_out"
                worker.retry_after = datetime.now(timezone.utc) + timedelta(seconds=settings.worker_failure_cooldown_seconds)
        except Exception as exc:
            worker.status = WorkerStatus.error
            worker.failure_kind = exc.kind if isinstance(exc, WorkerFailure) else classify_worker_failure(str(exc))
            if worker.failure_kind == "logged_out":
                update_account(worker, None)
            worker.retry_after = datetime.now(timezone.utc) + timedelta(seconds=settings.worker_failure_cooldown_seconds)
        await reconcile_worker(db, worker)
        await db.commit()



async def account_monitor_loop():
    import asyncio
    import logging
    from .database import SessionLocal
    settings = get_settings()
    slots = asyncio.Semaphore(4)

    async def inspect(worker_id):
        async with slots:
            await refresh_worker_account(worker_id)

    while True:
        try:
            async with SessionLocal() as db:
                ids = (await db.scalars(select(Worker.id).where(Worker.enabled.is_(True), Worker.endpoint != "removed://worker"))).all()
            results = await asyncio.gather(*(inspect(worker_id) for worker_id in ids), return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logging.getLogger(__name__).warning("Worker contribution check failed: %s", type(result).__name__)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception("Worker contribution monitor failed")
        await asyncio.sleep(settings.worker_recovery_interval_seconds)
