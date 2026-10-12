"""UTC calendar-month budgets, using settled historical request prices."""
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import Depends, HTTPException
from sqlalchemy import select, func
from .auth import ApiPrincipal, require_api_key
from .database import get_session
from .models import ApiKey, UsageRecord


def month_bounds(now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


async def monthly_usage(session, key_ids, now=None):
    if not key_ids:
        return {}
    start, end = month_bounds(now)
    rows = (await session.execute(select(
        UsageRecord.api_key_id, func.coalesce(func.sum(UsageRecord.cost_usd), 0)
    ).where(
        UsageRecord.api_key_id.in_(key_ids),
        UsageRecord.created_at >= start, UsageRecord.created_at < end,
    ).group_by(UsageRecord.api_key_id))).all()
    return {key_id: Decimal(amount) for key_id, amount in rows}


async def key_budget_display(session, keys):
    amounts = await monthly_usage(session, [key.id for key in keys])
    result = {}
    for key in keys:
        used = amounts.get(key.id, Decimal(0))
        limit = Decimal(key.monthly_limit_usd)
        percent = min(Decimal(100), max(Decimal(0), used / limit * 100)) if limit else Decimal(100)
        result[str(key.id)] = dict(used=format(used, ".2f"), limit=format(limit, ".2f"),
                                  percent=float(percent), exhausted=used >= limit)
    return result


async def require_key_budget(principal: ApiPrincipal = Depends(require_api_key),
                             session=Depends(get_session)):
    if principal.key_id is not None:
        key = await session.get(ApiKey, principal.key_id)
        used = (await monthly_usage(session, [principal.key_id])).get(principal.key_id, Decimal(0))
        if used >= key.monthly_limit_usd:
            raise HTTPException(429, detail={"error": {
                "message": "This API key has reached its monthly USD limit. The budget resets at the start of the next UTC month.",
                "type": "insufficient_quota", "code": "monthly_key_limit_exceeded",
            }})
    return principal
