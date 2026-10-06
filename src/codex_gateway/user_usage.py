"""Personal usage over thirty UTC calendar days, including today."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from .models import UsageRecord


async def recent_user_usage(db, username, *, now=None):
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=29)
    day = func.date(func.timezone("UTC", UsageRecord.created_at))
    rows = (await db.execute(select(
        day.label("day"),
        func.sum(UsageRecord.input_tokens + UsageRecord.output_tokens).label("tokens"),
        func.sum(UsageRecord.cost_usd).label("amount"),
        func.count().filter(UsageRecord.cost_usd.is_(None)).label("unpriced"),
    ).where(
        UsageRecord.owner_username == username,
        UsageRecord.created_at >= start,
        UsageRecord.created_at <= now,
    ).group_by(day).order_by(day))).all()
    by_day = {row.day.isoformat(): row for row in rows}
    daily = []
    for offset in range(30):
        date = (start + timedelta(days=offset)).date().isoformat()
        row = by_day.get(date)
        daily.append({"date": date, "tokens": int(row.tokens or 0) if row else 0})
    return {
        "start": start.date().isoformat(), "end": today.date().isoformat(),
        "tokens": sum(item["tokens"] for item in daily),
        "amount": sum((row.amount or Decimal(0) for row in rows), Decimal(0)),
        "unpriced": sum(row.unpriced for row in rows), "daily": daily,
    }
