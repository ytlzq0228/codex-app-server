"""Durable aggregate snapshots for state that cannot be rebuilt from request events."""
from .i18n import t
import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from .app_server import open_app_server
from .config import get_settings
from .database import SessionLocal
from .models import MetricSnapshot, SubscriptionPlan, Worker
from .rate_limits import summarize_windows, provider_usage
from .subscriptions import normalize_plan

STATES = {'healthy': '完全正常', 'limited': '登录 · 超限隔离',
          'service': '登录 · 服务异常隔离', 'logged_out': '未登录', 'other': '其他异常'}


def worker_state(worker):
    if worker.endpoint == 'removed://worker':
        return 'other'
    if not worker.auth_mode or worker.failure_kind == 'logged_out':
        return 'logged_out'
    if worker.status == 'error':
        if worker.failure_kind == 'limit':
            return 'limited'
        if worker.failure_kind in {'connection', 'runtime'}:
            return 'service'
        return 'other'
    if worker.enabled and worker.status in {'ready', 'busy'} and not worker.failure_kind:
        return 'healthy'
    return 'other'


def state_counts(workers):
    workers = [worker for worker in workers if worker.endpoint != 'removed://worker']
    counts = dict.fromkeys(STATES, 0)
    for worker in workers:
        counts[worker_state(worker)] += 1
    return {'version': 2, 'total': len(workers), 'counts': counts}


def _pool_usage(workers, weights, readings):
    eligible = [w for w in workers if w.endpoint != 'removed://worker' and w.auth_mode and w.failure_kind != 'logged_out']
    totals = {key: [0.0, 0.0, 0] for key in ('risk', 'five_hour', 'week')}
    worker_windows = {}
    total_weight = 0.0
    unknown_plans = 0
    for worker in eligible:
        from .subscriptions import plan_key
        plan = plan_key(worker.plan_type, getattr(worker, "provider", None) or "codex")
        weight = float(weights.get(plan, 1))
        unknown_plans += int(plan not in weights)
        total_weight += weight
        windows = {}
        reading = readings.get(str(worker.id), {})
        succeeded = isinstance(reading.get('buckets'), list) and (
            (getattr(worker, 'provider', None) or 'codex') == 'codex'
            or bool(reading.get('buckets')) or reading.get('unlimited') is True)
        for key in ('five_hour', 'week'):
            values = [b[key]['used'] for b in reading.get('buckets', [])
                      if b.get(key) and math.isfinite(b[key]['used'])]
            if values:
                windows[key] = max(values)
            elif succeeded:
                windows[key] = 0.0
        if windows:
            windows['risk'] = max(windows.values())
        week_used = windows.get('week')
        worker_windows[str(worker.id)] = {
            'week_used': week_used,
            'weight': weight,
            'weighted_remaining': (100.0 - week_used) * weight if week_used is not None else None,
        }
        for key, value in windows.items():
            totals[key][0] += value * weight
            totals[key][1] += weight
            totals[key][2] += 1
    return {'version': 3, 'eligible': len(eligible), 'total_weight': total_weight,
            'unknown_plans': unknown_plans, 'weights': {k: float(v) for k, v in weights.items()},
            'workers': worker_windows,
            'windows': {key: {'used': numerator / denominator if denominator else None,
                              'covered_weight': denominator, 'covered': count}
                        for key, (numerator, denominator, count) in totals.items()}}


def pool_usage(workers, weights, readings):
    # Preserve per-worker routing signals; never combine providers in the UI.
    result = _pool_usage(workers, weights, readings)
    result['version'] = 4
    result['providers'] = {
        provider: _pool_usage([w for w in workers if (getattr(w, 'provider', None) or 'codex') == provider], weights, readings)
        for provider in ('codex', 'gemini', 'claude')
    }
    return result


async def read_usage(worker, semaphore):
    settings = get_settings()
    async with semaphore:
        try:
            async with asyncio.timeout(25):
                if (getattr(worker, 'provider', None) or 'codex') == 'gemini':
                    from .gemini_backend import worker_rpc
                    return str(worker.id), provider_usage(await worker_rpc(worker.endpoint, settings, '/rate-limits'))
                if getattr(worker, 'provider', None) == 'claude':
                    from .gemini_backend import worker_rpc
                    return str(worker.id), summarize_windows(await worker_rpc(worker.endpoint, settings, '/rate-limits'))
                if (getattr(worker, 'provider', None) or 'codex') != 'codex':
                    return str(worker.id), {}
                async with open_app_server(worker.endpoint, settings.app_server_token.get_secret_value(), 20) as server:
                    payload = await server.call('account/rateLimits/read', {'excludeResetCreditDetails': True})
                return str(worker.id), summarize_windows(payload)
        except Exception:
            return str(worker.id), {}


async def record_snapshot(metric, minutes, now):
    bucket = now.replace(minute=now.minute // minutes * minutes, second=0, microsecond=0)
    async with SessionLocal() as db:
        # Transaction-scoped lock prevents duplicate polling across gateway processes.
        lock = 724910 if metric == 'worker_states' else 724911
        if not await db.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': lock}):
            return
        existing = await db.get(MetricSnapshot, (metric, bucket))
        if existing and (metric != 'subscription_usage' or existing.payload.get('version', 0) >= 4):
            return
        workers = (await db.scalars(select(Worker))).all()
        if metric == 'worker_states':
            payload = state_counts(workers)
        else:
            weights = {p.name: p.weight for p in (await db.scalars(select(SubscriptionPlan))).all()}
            semaphore = asyncio.Semaphore(4)
            tasks = [asyncio.create_task(read_usage(w, semaphore)) for w in workers
                     if w.endpoint != 'removed://worker' and w.auth_mode and w.failure_kind != 'logged_out']
            readings = {}
            if tasks:
                try:
                    done, pending = await asyncio.wait(tasks, timeout=55)
                    readings = dict(task.result() for task in done)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            payload = pool_usage(workers, weights, readings)
        if existing:
            existing.observed_at, existing.payload = now, payload
        else:
            await db.execute(insert(MetricSnapshot).values(metric=metric, bucket_at=bucket,
                             observed_at=now, payload=payload).on_conflict_do_nothing())
        await db.commit()


async def monitoring_loop():
    while True:
        for metric, minutes in (('worker_states', 10), ('subscription_usage', 60)):
            try:
                await record_snapshot(metric, minutes, datetime.now(timezone.utc))
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception('Monitoring snapshot failed: %s', metric)
        now = datetime.now(timezone.utc)
        await asyncio.sleep(60 - now.second)


async def monitoring_data(db, days):
    now = datetime.now(timezone.utc)
    rows = (await db.scalars(select(MetricSnapshot).where(
        MetricSnapshot.metric.in_(('worker_states', 'subscription_usage')),
        MetricSnapshot.bucket_at >= now - timedelta(days=days)).order_by(MetricSnapshot.bucket_at))).all()
    latest = await db.scalar(select(MetricSnapshot).where(MetricSnapshot.metric == 'subscription_usage')
                             .order_by(MetricSnapshot.bucket_at.desc()).limit(1))
    def serialize(row):
        return {'at': row.bucket_at.isoformat(), 'observed_at': row.observed_at.isoformat(), 'data': row.payload}
    return {'states': state_counts((await db.scalars(select(Worker))).all()), 'labels': {key: t(label) for key, label in STATES.items()},
            'current_usage': serialize(latest) if latest else None,
            'history': {metric: [serialize(r) for r in rows if r.metric == metric]
                        for metric in ('worker_states', 'subscription_usage')}}
