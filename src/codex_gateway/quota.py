"""Enabled-key capacity. All quota mutations serialize before reading capacity."""
from sqlalchemy import and_, or_, func, select, text
from fastapi import HTTPException
from .models import ApiKey, User, Worker, WorkerStatus


async def quota_lock(db):
    # A transaction-scoped advisory lock covers creation, enabling, transfer and
    # worker credit changes across gateway processes. Never commit inside helpers.
    with db.no_autoflush:
        await db.execute(text("SELECT pg_advisory_xact_lock(724193228)"))


def contribution_filters(username=None):
    plan = func.nullif(func.lower(func.trim(Worker.plan_type)), "")
    filters = [Worker.enabled.is_(True), Worker.endpoint != "removed://worker",
               # Usage exhaustion pauses routing, but retains paid-account credit.
               or_(Worker.status.in_([WorkerStatus.ready, WorkerStatus.busy]),
                   and_(Worker.status == WorkerStatus.error, Worker.failure_kind == "limit")),
               or_(and_(Worker.provider == "codex", Worker.auth_mode == "chatgpt"),
                   and_(Worker.provider == "gemini", Worker.auth_mode == "google-subscription")),
               plan.is_not(None), plan != "free",
               or_(Worker.provider != "gemini", ~plan.like("%free%")),
               Worker.account_checked_at.is_not(None),
               func.nullif(func.lower(func.trim(Worker.account_email)), "").is_not(None)]
    if username is not None:
        filters.append(Worker.owner_username == username)
    return filters


async def credited_workers(db, username=None):
    rows = (await db.execute(select(Worker.id, Worker.owner_username, Worker.provider,
        func.lower(func.trim(Worker.account_email))).where(*contribution_filters(username))
        .order_by(Worker.created_at, Worker.id))).all()
    seen, credited, duplicates = set(), set(), set()
    for worker_id, owner, provider, account in rows:
        identity = (owner, provider or "codex", account)
        if identity in seen:
            duplicates.add(worker_id)
        else:
            seen.add(identity)
            credited.add(worker_id)
    return credited, duplicates


async def quota_summary(db, username):
    base = await db.scalar(select(User.quota_granted).where(User.username == username)) or 0
    credited, _ = await credited_workers(db, username)
    credits = len(credited)
    used = await db.scalar(select(func.count()).select_from(ApiKey).where(ApiKey.owner_username == username, ApiKey.enabled.is_(True), ApiKey.deleted_at.is_(None))) or 0
    return dict(granted=base, contributed=credits, total=base+credits, used=used, available=max(0,base+credits-used))


async def enforce_quota(db, username):
    if not username:
        return 0
    await quota_lock(db)
    await db.flush()
    quota = await quota_summary(db, username)
    excess = quota['used'] - quota['total']
    if excess <= 0:
        return 0
    keys = (await db.scalars(select(ApiKey).where(ApiKey.owner_username == username, ApiKey.enabled.is_(True), ApiKey.deleted_at.is_(None))
        .order_by(ApiKey.last_used_at.asc().nullsfirst(), ApiKey.created_at.asc(), ApiKey.id.asc()).limit(excess).with_for_update())).all()
    for key in keys:
        key.enabled = False
    await db.flush()
    return len(keys)


async def ensure_capacity(db, username, exclude_key_id=None):
    await quota_lock(db)
    user = await db.scalar(select(User).where(User.username == username).execution_options(populate_existing=True))
    if not user or not user.enabled:
        raise HTTPException(400, "请选择已存在且启用的用户")
    await enforce_quota(db, username)
    quota = await quota_summary(db, username)
    used = quota['used']
    if exclude_key_id:
        same = await db.scalar(select(ApiKey.id).where(ApiKey.id == exclude_key_id, ApiKey.owner_username == username, ApiKey.enabled.is_(True), ApiKey.deleted_at.is_(None)))
        used -= bool(same)
    if used >= quota['total']:
        raise HTTPException(409, "Quota 不足，请停用其他 Key、贡献有效付费 Worker 或联系管理员增加额度")
    return user


async def reconcile_worker(db, worker, previous_owner=None):
    await quota_lock(db)
    await db.flush()
    from .subscriptions import remember_plan
    await remember_plan(db, worker.plan_type, worker.provider or "codex")
    for owner in sorted({name for name in (worker.owner_username, previous_owner) if name}):
        await enforce_quota(db, owner)
