"""Current monthly subscription estimates, separate from historical actual costs."""
from decimal import Decimal
import re
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from .models import SubscriptionPlan, Worker

DEFAULT_PLAN_COLOR = '#16734a'
PLAN_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')


def normalize_plan(name):
    return (name or '').strip().lower()


def normalize_plan_color(color):
    if not isinstance(color, str) or not PLAN_COLOR_RE.fullmatch(color.strip()):
        raise ValueError('套餐颜色必须是六位十六进制色值')
    return color.strip().lower()


def safe_plan_color(color):
    try:
        return normalize_plan_color(color)
    except ValueError:
        return DEFAULT_PLAN_COLOR


def plan_pill_style(color):
    color = safe_plan_color(color)
    red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
    foreground = '#111827' if (red * 299 + green * 587 + blue * 114) / 1000 >= 150 else '#ffffff'
    return f'background-color:{color};color:{foreground}'


def plan_key(name, provider="codex"):
    name = normalize_plan(name)
    return name if not name or provider == "codex" else provider + ":" + name


def plan_label(name):
    provider, sep, value = name.partition(":")
    if sep and provider in {"gemini", "claude"}:
        return {"gemini": "Gemini", "claude": "Claude"}[provider] + " · " + value
    return "OpenAI · " + name


async def remember_plan(db, name, provider="codex"):
    name = plan_key(name, provider)
    if name:
        await db.execute(insert(SubscriptionPlan).values(name=name).on_conflict_do_nothing(index_elements=['name']))


async def subscription_summary(db):
    workers = (await db.scalars(select(Worker))).all()
    # Also discover legacy/manual worker records, retaining plans after logout.
    for worker in workers:
        await remember_plan(db, worker.plan_type, worker.provider or "codex")
    plans = (await db.scalars(select(SubscriptionPlan).order_by(SubscriptionPlan.name))).all()
    rows = {p.name: dict(name=p.name, label=plan_label(p.name), price=p.monthly_price, weight=p.weight,
        color=safe_plan_color(p.color), style=plan_pill_style(p.color), count=0, subtotal=Decimal(0)) for p in plans}
    unknown = 0
    for worker in workers:
        if worker.endpoint == 'removed://worker' or not worker.auth_mode or worker.failure_kind == 'logged_out':
            continue
        name = plan_key(worker.plan_type, worker.provider or "codex")
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
