"""Enabled-key capacity. All quota mutations serialize before reading capacity."""
from sqlalchemy import and_, or_, func, select, text
from fastapi import HTTPException
from .models import ApiKey, ContributionCredit, User, Worker, WorkerStatus


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
                   and_(Worker.provider == "gemini", Worker.auth_mode == "google-subscription"),
                   and_(Worker.provider == "claude", Worker.auth_mode == "claude-subscription")),
               plan.is_not(None), plan != "free",
               or_(Worker.provider != "gemini", ~plan.like("%free%")),
               Worker.account_checked_at.is_not(None),
               func.nullif(func.lower(func.trim(Worker.account_email)), "").is_not(None)]
    if username is not None:
        filters.append(Worker.owner_username == username)
    return filters


def account_identity(provider, email):
    return (provider or "codex", (email or "").strip().lower())


def prefers(owner, email):
    """The account's own user: username equals the email local part."""
    return bool(owner) and email.split("@", 1)[0] == owner.strip().lower()


async def sync_credits(db, claimant=None):
    """Assign each eligible upstream account's single credit; caller holds quota_lock.

    A credit is released when its Worker stops qualifying (logout, deletion,
    failure other than usage limits, account change). A free credit goes to the
    owner whose username matches the email prefix, otherwise to the Worker that
    claims it first (`claimant`, the Worker just reconciled). A non-matching
    holder keeps the credit until a matching owner's Worker qualifies.
    Returns the owners whose credited Workers changed.
    """
    rows = (await db.execute(select(Worker.id, Worker.owner_username, Worker.provider, Worker.account_email)
        .where(*contribution_filters()).order_by(Worker.created_at, Worker.id))).all()
    eligible = {row.id: (row.owner_username, account_identity(row.provider, row.account_email)) for row in rows}
    candidates = {}
    for worker_id, (owner, identity) in eligible.items():
        candidates.setdefault(identity, []).append(worker_id)
    claims = (await db.scalars(select(ContributionCredit))).all()
    holder_owners = dict((await db.execute(select(Worker.id, Worker.owner_username)
        .where(Worker.id.in_([claim.worker_id for claim in claims])))).all()) if claims else {}
    changed, held = set(), {}
    for claim in claims:
        identity = (claim.provider, claim.account_email)
        if eligible.get(claim.worker_id, (None, None))[1] == identity:
            held[identity] = claim
        else:
            changed.add(holder_owners.get(claim.worker_id))
            await db.delete(claim)
    await db.flush()
    claimant_id = getattr(claimant, "id", None)
    for identity, worker_ids in candidates.items():
        email = identity[1]
        preferred = [worker_id for worker_id in worker_ids if prefers(eligible[worker_id][0], email)]
        claim = held.get(identity)
        if claim and (claim.worker_id in preferred or not preferred):
            continue
        pool = preferred or worker_ids
        winner = claimant_id if claimant_id in pool else pool[0]
        if claim:
            changed.update({eligible[claim.worker_id][0], eligible[winner][0]})
            claim.worker_id = winner
        else:
            changed.add(eligible[winner][0])
            db.add(ContributionCredit(provider=identity[0], account_email=email, worker_id=winner))
    await db.flush()
    changed.discard(None)
    return changed


async def credited_workers(db, username=None):
    """Credited Workers hold their account's system-wide claim; other eligible ones are duplicates."""
    eligible = (await db.scalars(select(Worker.id).where(*contribution_filters(username))
        .order_by(Worker.created_at, Worker.id))).all()
    credited = set((await db.scalars(select(Worker.id).join(ContributionCredit, ContributionCredit.worker_id == Worker.id)
        .where(*contribution_filters(username),
               ContributionCredit.provider == func.coalesce(Worker.provider, "codex"),
               ContributionCredit.account_email == func.lower(func.trim(Worker.account_email))))).all())
    return credited, {worker_id for worker_id in eligible if worker_id not in credited}


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
    # Claims normally move in reconcile_worker; re-sync here so capacity never
    # depends on a Worker change that skipped reconciliation.
    for owner in sorted(await sync_credits(db) - {username}):
        await enforce_quota(db, owner)
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
    changed = await sync_credits(db, claimant=worker)
    for owner in sorted({name for name in (worker.owner_username, previous_owner, *changed) if name}):
        await enforce_quota(db, owner)
