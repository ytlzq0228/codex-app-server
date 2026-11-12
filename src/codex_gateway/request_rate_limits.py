"""Cluster-wide rolling request limits, independent of billing and request outcome."""
import math
from contextvars import ContextVar

from fastapi import Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from .auth import ApiPrincipal, require_api_key
from .config import get_settings
from .database import SessionLocal
from .models import RequestRateWindow

_request_key = ContextVar("rate_limit_request_key", default=None)


def record_attempt(attempts, instant, limit, *, worker=False):
    """Worker windows count admitted attempts; Key windows also retain rejections."""
    recent = [stamp for stamp in attempts if stamp > instant - 60]
    waits = []
    if len(recent) >= limit:
        waits.append(recent[-limit] + 60 - instant)
    if worker:
        burst = max(1, math.ceil(limit / 6))
        short = [stamp for stamp in recent if stamp > instant - 10]
        if len(short) >= burst:
            waits.append(short[-burst] + 10 - instant)
        if waits:
            return recent, max(1, math.ceil(max(waits)))
    rejected = bool(waits)
    recent = (recent + [instant])[-limit:]
    retry_after = max(1, math.ceil(recent[0] + 60 - instant)) if rejected else 0
    return recent, retry_after


async def consume(scope, limit):
    if not limit:
        return
    # Commit even when rejected, and never roll back a debit with the request.
    # A single row per identity bounds storage; no per-request event table.
    async with SessionLocal() as session, session.begin():
        await session.execute(insert(RequestRateWindow).values(scope=scope, attempts=[])
                              .on_conflict_do_nothing(index_elements=["scope"]))
        row = await session.scalar(select(RequestRateWindow)
                                   .where(RequestRateWindow.scope == scope).with_for_update())
        # Read after acquiring the lock: app clocks and lock wait time cannot
        # create extra allowance on another gateway node.
        instant = float(await session.scalar(text("SELECT EXTRACT(EPOCH FROM clock_timestamp())")))
        row.attempts, retry_after = record_attempt(
            row.attempts, instant, limit, worker=scope.startswith(("worker:", "worker-dispatch:")))
    if retry_after:
        kind = scope.split(":", 1)[0].replace("-dispatch", "")
        if kind == "worker" and _request_key.get() is not None:
            await set_key_cooldown(_request_key.get(), retry_after)
        raise HTTPException(429, detail={"error": {
            "message": f"RATE LIMIT: {kind} admission budget exhausted (limit {limit}/60s"
                       + (f", {max(1, math.ceil(limit / 6))}/10s" if kind == "worker" else "")
                       + f"); wait at least {retry_after} seconds before retrying.",
            "type": "rate_limit_error", "code": f"{kind}_rate_limit_exceeded", "param": None,
        }}, headers={"Retry-After": str(retry_after)})


async def count_key_attempt(principal):
    await consume(f"key:{principal.key_id or 'development'}", get_settings().key_requests_per_minute)
    await check_key_cooldown(principal.key_id or 'development')


async def count_worker_attempt(target):
    if target.worker_id:
        await consume(f"worker:{target.worker_id}", get_settings().worker_requests_per_minute)


async def require_key_rate_limit(principal: ApiPrincipal = Depends(require_api_key)):
    token = _request_key.set(principal.key_id or 'development')
    try:
        await count_key_attempt(principal)
        yield principal
    finally:
        _request_key.reset(token)


async def admit_worker_dispatch(target):
    """Final shared gate after local queueing, immediately before transport.

    Separate from ingress admission: never refund a failed transport attempt.
    Both gates enforce the same per-Worker budget independently.
    """
    if target.worker_id:
        if _request_key.get() is not None:
            await check_key_cooldown(_request_key.get())
        await consume(f"worker-dispatch:{target.worker_id}", get_settings().worker_requests_per_minute)


async def set_key_cooldown(key, seconds):
    """Persist Worker backpressure for this Key across gateway nodes/restarts."""
    scope = f"key-cooldown:{key}"
    async with SessionLocal() as db, db.begin():
        await db.execute(insert(RequestRateWindow).values(scope=scope, attempts=[])
                         .on_conflict_do_nothing(index_elements=["scope"]))
        row = await db.scalar(select(RequestRateWindow)
                              .where(RequestRateWindow.scope == scope).with_for_update())
        now = float(await db.scalar(text("SELECT EXTRACT(EPOCH FROM clock_timestamp())")))
        row.attempts = [max(row.attempts[0] if row.attempts else 0, now + seconds)]


async def check_key_cooldown(key):
    # This is a read, not a new reservation. Polling never prolongs the deadline.
    async with SessionLocal() as db:
        stamps = await db.scalar(select(RequestRateWindow.attempts)
                                 .where(RequestRateWindow.scope == f"key-cooldown:{key}"))
        if not stamps:
            return
        now = float(await db.scalar(text("SELECT EXTRACT(EPOCH FROM clock_timestamp())")))
    wait = math.ceil(stamps[0] - now)
    if wait > 0:
        raise HTTPException(429, detail={"error": {
            "message": f"RATE LIMIT: Key is cooling down; wait {wait} seconds before retrying.",
            "type": "rate_limit_error", "code": "key_retry_cooldown", "param": None,
        }}, headers={"Retry-After": str(wait)})
