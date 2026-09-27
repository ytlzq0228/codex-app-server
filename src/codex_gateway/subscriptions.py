"""Current monthly subscription estimates, separate from historical actual costs."""
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from .models import SubscriptionPlan, Worker


def normalize_plan(name):
    return (name or '').strip().lower()


async def remember_plan(db, name):
    name = normalize_plan(name)
    if name:
        await db.execute(insert(SubscriptionPlan).values(name=name).on_conflict_do_nothing(index_elements=['name']))


async def subscription_summary(db):
    workers = (await db.scalars(select(Worker))).all()
    # Also discover legacy/manual worker records, retaining plans after logout.
    for name in sorted({normalize_plan(w.plan_type) for w in workers} - {''}):
        await remember_plan(db, name)
    plans = (await db.scalars(select(SubscriptionPlan).order_by(SubscriptionPlan.name))).all()
    rows = {p.name: dict(name=p.name, price=p.monthly_price, weight=p.weight, count=0, subtotal=Decimal(0)) for p in plans}
    unknown = 0
    for worker in workers:
        if worker.endpoint == 'removed://worker' or not worker.auth_mode or worker.failure_kind == 'logged_out':
            continue
        name = normalize_plan(worker.plan_type)
        if not name:
            unknown += 1
            continue
        row = rows[name]
        row['count'] += 1
        if row['price'] is not None:
            row['subtotal'] += row['price']
    unpriced = unknown + sum(row['count'] for row in rows.values() if row['price'] is None)
    return dict(rows=list(rows.values()), total=sum((row['subtotal'] for row in rows.values()), Decimal(0)),
                count=unknown+sum(row['count'] for row in rows.values()), unknown=unknown, unpriced=unpriced)
