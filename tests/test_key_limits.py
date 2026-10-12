from datetime import datetime, timezone, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from codex_gateway.auth import ApiPrincipal
from codex_gateway.config import Settings
from codex_gateway.key_limits import month_bounds, monthly_usage, require_key_budget, key_budget_display
from codex_gateway.models import ApiKey, UsageRecord


@pytest.fixture
def budget_db():
    engine = create_engine("sqlite://")
    ApiKey.__table__.create(engine)
    UsageRecord.__table__.create(engine)
    with Session(engine) as db:
        class AsyncSession:
            async def execute(self, statement):
                return db.execute(statement)
            async def get(self, model, key):
                return db.get(model, key)
        yield db, AsyncSession()
    engine.dispose()


def test_month_bounds_year_rollover_and_utc_conversion():
    start, end = month_bounds(datetime(2026, 12, 31, 23, tzinfo=timezone.utc))
    assert start == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert month_bounds(datetime(2026, 3, 1, 1, tzinfo=timezone(timedelta(hours=8))))[0].month == 2


@pytest.mark.asyncio
async def test_monthly_totals_include_499_but_exclude_other_months_and_keys(budget_db):
    db, session = budget_db
    key_id, other = uuid4(), uuid4()
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    start, end = month_bounds(now)
    for key, cost, timestamp, status in [
        (key_id, '12.123456', start, 200),
        (key_id, '4.5', now, 499),
        (key_id, '0', now, 200),  # superseded Claude provisional record
        (key_id, None, now, 200),
        (key_id, '100', start - timedelta(microseconds=1), 200),
        (key_id, '100', end, 200),
        (other, '100', now, 200),
    ]:
        db.add(UsageRecord(api_key_id=key, cost_usd=Decimal(cost) if cost else None,
                           created_at=timestamp, status_code=status,
                           model='test', request_id=str(uuid4())))
    db.commit()
    totals = await monthly_usage(session, [key_id], now)
    assert totals[key_id] == Decimal('16.623456')
    assert other not in totals
    assert await monthly_usage(session, [], now) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('limit,amount,blocked', [('30','29.99',False),('30','30',True),('30','31',True),('0','0',True)])
async def test_enforcement_and_display(budget_db, limit, amount, blocked):
    db, session = budget_db
    key = ApiKey(name='test', prefix=uuid4().hex[:20], key_hash='hash', monthly_limit_usd=Decimal(limit))
    db.add(key); db.flush()
    db.add(UsageRecord(api_key_id=key.id, model='test', status_code=499,
                       request_id=str(uuid4()), created_at=datetime.now(timezone.utc), cost_usd=Decimal(amount)))
    db.commit()
    principal = ApiPrincipal(key.id, 'test')
    if blocked:
        with pytest.raises(HTTPException) as exc:
            await require_key_budget(principal, session)
        assert exc.value.status_code == 429
        assert exc.value.detail['error']['code'] == 'monthly_key_limit_exceeded'
    else:
        assert await require_key_budget(principal, session) == principal
    display = (await key_budget_display(session, [key]))[str(key.id)]
    assert display['exhausted'] == blocked
    assert 0 <= display['percent'] <= 100
    assert display['limit'] == format(Decimal(limit), '.2f')


def test_configurable_new_key_default(budget_db, monkeypatch):
    from codex_gateway import config
    db, _ = budget_db
    monkeypatch.setattr(config, 'get_settings', lambda: Settings(default_key_monthly_limit_usd='4321.50'))
    key = ApiKey(name='new', prefix='new', key_hash='hash')
    db.add(key); db.commit()
    assert key.monthly_limit_usd == Decimal('4321.50')
    for bad in ['-1', 'NaN', 'Infinity', '0.001']:
        with pytest.raises(ValueError):
            Settings(default_key_monthly_limit_usd=bad)


@pytest.mark.asyncio
async def test_month_reset_uses_new_month(budget_db):
    db, session = budget_db
    key_id = uuid4()
    db.add(UsageRecord(api_key_id=key_id, model='test', status_code=200, request_id='old',
                       created_at=datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc), cost_usd=3000))
    db.commit()
    assert await monthly_usage(session, [key_id], datetime(2026, 11, 1, tzinfo=timezone.utc)) == {}
