"""Read-only PRTG HTTP Data Advanced projections, local capacity/global quota."""
import asyncio
import hmac
import logging
import math
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, func

from .config import get_settings
from .database import SessionLocal
from .models import AppNode, MetricSnapshot, UsageRecord, Worker
from .monitoring import state_counts, worker_state

router = APIRouter(prefix='/monitoring/prtg', include_in_schema=False)


def authorize(authorization: str | None = Header(default=None)):
    token = get_settings().monitoring_token.get_secret_value()
    if len(token) < 32:
        raise HTTPException(503, 'Monitoring token is not configured')
    if not hmac.compare_digest(authorization or '', 'Bearer ' + token):
        raise HTTPException(401, 'Unauthorized')


def channel(name, value, unit='Count', custom=None, **limits):
    if value is None or not math.isfinite(float(value)):
        raise ValueError('Unavailable metric: ' + name)
    result = {'channel': name, 'value': value, 'unit': unit, 'float': 1 if isinstance(value, float) else 0}
    if custom:
        result['customunit'] = custom
    if limits:
        result.update(limits, limitmode=1)
    return result


def quota_channels(payload, age):
    if age > 900:
        raise ValueError('Global quota snapshot is older than 15 minutes')
    rows = [channel('Snapshot age', age, 'TimeSeconds', limitmaxwarning=600)]
    total = payload.get('total_weight', 0)
    if not total:
        raise ValueError('No eligible provider accounts')
    for key, label in [('five_hour', '5 hour'), ('week', 'Week'), ('risk', 'Most constrained')]:
        window = payload.get('windows', {}).get(key, {})
        used = window.get('used')
        if used is None:
            raise ValueError('No valid quota samples for ' + label)
        rows += [channel(label + ' remaining', float(100 - used), 'Percent', limitminwarning=20, limitminerror=20),
                 channel(label + ' coverage', float(window.get('covered_weight', 0) / total * 100), 'Percent', limitminwarning=50, limitminerror=20)]
    return rows


async def manager_data(settings, path):
    url = settings.node_manager_url if settings.node_id else settings.manager_url
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url.rstrip('/') + path, headers={'Authorization': 'Bearer ' + settings.manager_token.get_secret_value()})
        response.raise_for_status()
        return response.json()


async def throughput(db, settings, now):
    # Attribute the single ingress audit record to the Worker owner, even for
    # cross-node forwarding. Project only counters and call IDs, never content.
    query = select(UsageRecord.input_tokens, UsageRecord.output_tokens,
                   UsageRecord.status_code, UsageRecord.conversation_evidence['client_tool_call_ids']).join(Worker, Worker.id == UsageRecord.worker_id).where(
                       UsageRecord.created_at >= now - timedelta(seconds=60), UsageRecord.created_at <= now)
    if settings.node_id:
        query = query.where(Worker.node_id == settings.node_id)
    records = (await db.execute(query)).all()
    calls = {call for row in records for call in (row[3] or []) if isinstance(call, str)}
    return [channel('Completed requests', len(records) / 60, 'Custom', 'requests/s'),
            channel('Input tokens', sum(r[0] for r in records) / 60, 'Custom', 'tokens/s'),
            channel('Output tokens', sum(r[1] for r in records) / 60, 'Custom', 'tokens/s'),
            channel('Tool calls', len(calls) / 60, 'Custom', 'calls/s'),
            channel('Failed requests', sum(r[2] >= 400 for r in records) / 60, 'Custom', 'requests/s')]


async def local_channels(group, request, settings):
    from .cluster import local_routing_load
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        if group in ('codex', 'gemini', 'claude'):
            sample = await db.scalar(select(MetricSnapshot).where(MetricSnapshot.metric == 'subscription_usage').order_by(MetricSnapshot.bucket_at.desc()).limit(1))
            if not sample:
                raise ValueError('Global quota snapshot unavailable')
            return quota_channels(sample.payload.get('providers', {}).get(group, {}), max(0, int((now - sample.observed_at).total_seconds())))
        if group == 'throughput':
            return await throughput(db, settings, now)
        if group == 'health':
            await db.execute(select(func.now()))
            age = 0
            if settings.node_id:
                node = await db.get(AppNode, settings.node_id)
                if not node:
                    raise ValueError('Node heartbeat unavailable')
                age = max(0, int((now - node.heartbeat_at).total_seconds()))
            await db.rollback()
            await manager_data(settings, '/healthz')
            return [channel('Database reachable', 1), channel('Manager reachable', 1),
                    channel('Heartbeat age', age, 'TimeSeconds', limitmaxerror=settings.node_timeout_seconds)]
        query = select(Worker).where(Worker.endpoint != 'removed://worker')
        if settings.node_id:
            query = query.where(Worker.node_id == settings.node_id)
        workers = (await db.scalars(query)).all()
    if group == 'workers':
        counts = state_counts(workers)
        return [channel('Total workers', counts['total'])] + [channel(key, value) for key, value in counts['counts'].items()]
    backend = request.app.state.backend
    load = (await local_routing_load(backend, ''))['active']
    rows = []
    for provider, maximum in [('codex', settings.max_ws_per_worker), ('gemini', 4), ('claude', 40)]:
        own = [w for w in workers if w.provider == provider]
        usable = [w for w in own if worker_state(w) == 'healthy']
        occupied = sum(load.get(str(w.id), 0) for w in own)
        free = sum(max(0, maximum - load.get(str(w.id), 0)) for w in usable)
        rows += [channel(provider + ' configured slots', len(own) * maximum),
                 channel(provider + ' usable slots', len(usable) * maximum),
                 channel(provider + ' in-flight turns', occupied), channel(provider + ' free usable slots', free)]
    runs = getattr(getattr(backend, 'tool_sessions', None), 'runs', ())
    waiting = sum(run.reply is not None and not run.reply.done() for run in list(runs))
    rows += [channel('In-flight turns including tool waits', sum(load.values())), channel('Waiting for tool results', waiting)]
    host = await manager_data(settings, '/monitoring/host')
    estimate = min(int(host['cpu_cores'] * .8 // 2), int(host['memory_total_bytes'] * .8 // (4 * 1024**3)))
    rows += [channel('Estimated worker capacity', estimate), channel('Current workers', len(workers)),
             channel('Estimated additional workers', max(0, estimate - len(workers)))]
    return rows


@router.get('/{group}', dependencies=[Depends(authorize)])
async def sensor(group: str, request: Request):
    if group not in {'health', 'workers', 'capacity', 'throughput', 'system', 'codex', 'gemini', 'claude'}:
        raise HTTPException(404, 'Unknown sensor')
    settings = get_settings()
    try:
        async with asyncio.timeout(15):
            if group == 'system':
                data = await manager_data(settings, '/monitoring/host')
                rows = [channel('Host CPU', data['cpu_percent'], 'Percent'),
                        channel('Host memory', data['memory_percent'], 'Percent', limitmaxwarning=85, limitmaxerror=95),
                        channel('Root disk used', data['disk_percent'], 'Percent', limitmaxwarning=85, limitmaxerror=95),
                        channel('Host load 1 minute', data['load1'], 'Custom', 'load'),
                        channel('Network receive', data['rx_bytes_per_second'], 'Custom', 'bytes/s'),
                        channel('Network transmit', data['tx_bytes_per_second'], 'Custom', 'bytes/s')]
            else:
                rows = await local_channels(group, request, settings)
        scope = 'global replicated quota' if group in {'codex', 'gemini', 'claude'} else settings.node_id or 'local'
        note = '; resource estimate: 80% host resources, 2 CPU/4 GiB per worker; not an admission limit' if group == 'capacity' else ''
        if group == 'throughput':
            note = '; last 60s completed audit records by worker owner, settled tokens; not live token generation'
        payload = {'prtg': {'result': rows, 'text': scope + note}}
    except ValueError as exc:
        payload = {'prtg': {'error': 1, 'text': str(exc)}}
    except Exception:
        logging.getLogger(__name__).exception('PRTG sampling failed: %s', group)
        payload = {'prtg': {'error': 1, 'text': 'Monitoring data unavailable; inspect node logs'}}
    return JSONResponse(payload, headers={'Cache-Control': 'no-store'})
