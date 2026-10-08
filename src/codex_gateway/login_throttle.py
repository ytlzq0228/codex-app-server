"""Lockout for password login, counted per (account, address) and per address.

The (account, address) counter stops guessing against one user without letting
a remote party lock that account for everyone; the address counter bounds
credential stuffing that spreads a few guesses over many accounts. A successful
login clears both, so a legitimate user is never locked out by a noisy neighbour
behind the same address.
"""
from .i18n import t
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from .models import LoginFailure

# Failures tolerated before the first lock, by scope prefix.
ALLOWANCE = {"user": 5, "addr": 20}
FIRST_LOCK_SECONDS = 15
MAX_LOCK_SECONDS = 900
# Counters idle for this long are dropped, so occasional typos never accumulate.
WINDOW_SECONDS = 3600


def now():
    return datetime.now(timezone.utc)


def scopes(request, username):
    client = request.client
    address = (client.host if client and client.host else "unknown")[:45]
    # The account counter is scoped to the caller's address: guesses from one
    # address cannot lock the same account out for everyone else.
    return [f"user:{username.strip().lower()[:120]}|{address}", f"addr:{address}"]


def lock_seconds(scope, failures):
    excess = failures - ALLOWANCE[scope.split(":", 1)[0]]
    return min(MAX_LOCK_SECONDS, FIRST_LOCK_SECONDS * 2 ** max(0, excess))


async def guard(db, keys):
    """Reject the attempt while any counter for this login is still locked."""
    instant = now()
    rows = (await db.scalars(select(LoginFailure).where(LoginFailure.scope.in_(keys)))).all()
    locked = [row.locked_until for row in rows if row.locked_until and row.locked_until > instant]
    if not locked:
        return
    retry = max(1, int((max(locked) - instant).total_seconds()))
    raise HTTPException(429, t('登录尝试过于频繁，请稍后再试'), headers={"Retry-After": str(retry)})


async def record_failure(db, keys):
    instant = now()
    await db.execute(delete(LoginFailure).where(
        LoginFailure.last_failed_at <= instant - timedelta(seconds=WINDOW_SECONDS)))
    for scope in keys:
        # Concurrent attempts must increment rather than collide on the primary key.
        statement = insert(LoginFailure).values(
            scope=scope, failures=1, first_failed_at=instant, last_failed_at=instant)
        await db.execute(statement.on_conflict_do_update(
            index_elements=["scope"],
            set_={"failures": LoginFailure.__table__.c.failures + 1, "last_failed_at": instant}))
    rows = (await db.scalars(select(LoginFailure).where(LoginFailure.scope.in_(keys))
                             .execution_options(populate_existing=True))).all()
    for row in rows:
        if row.failures >= ALLOWANCE[row.scope.split(":", 1)[0]]:
            row.locked_until = instant + timedelta(seconds=lock_seconds(row.scope, row.failures))
    await db.commit()


async def clear(db, keys):
    await db.execute(delete(LoginFailure).where(LoginFailure.scope.in_(keys)))
    await db.commit()
