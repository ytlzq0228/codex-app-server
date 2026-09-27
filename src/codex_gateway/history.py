"""Shared, owner-scoped conversation pagination for admin and user history."""
from decimal import Decimal

from sqlalchemy import String, cast, func, literal, select

from .models import ApiKey, ResponseBinding, UsageRecord, Worker


async def conversation_history(db, *, owner=None, page=1, page_size=30, filters=(), status='', conversation_id='', key_id='', endpoint=''):
    # Owner scope is applied before ranking AND when fetching detail rows.
    scope = [UsageRecord.owner_username == owner] if owner is not None else []
    conversation = func.coalesce(UsageRecord.logical_conversation_id, UsageRecord.thread_id,
                                 ResponseBinding.thread_id, UsageRecord.request_id)
    group_key = (func.coalesce(cast(UsageRecord.api_key_id, String), literal('development'))
                 + literal(':') + func.coalesce(UsageRecord.endpoint, literal('unknown'))
                 + literal(':') + conversation)
    if key_id:
        scope.append(func.coalesce(cast(UsageRecord.api_key_id, String), literal('development')) == key_id)
    if endpoint:
        scope.append(func.coalesce(UsageRecord.endpoint, literal('unknown')) == endpoint)
    if conversation_id:
        scope.append(conversation == conversation_id)
    source = select(
        UsageRecord.id.label('id'), group_key.label('group_key'),
        conversation.label('conversation'),
        func.coalesce(UsageRecord.thread_id, ResponseBinding.thread_id).label('worker_thread'), UsageRecord.created_at.label('created_at'),
        UsageRecord.status_code.label('status_code'),
        func.row_number().over(partition_by=group_key,
            order_by=(UsageRecord.created_at.desc(), UsageRecord.id.desc())).label('position'),
    ).select_from(UsageRecord).outerjoin(ResponseBinding, UsageRecord.request_id == ResponseBinding.response_id).where(*scope)
    ranked = source.subquery()
    eligible = select(ranked.c.group_key, ranked.c.created_at.label('latest_at')).where(ranked.c.position == 1)
    # Search/date/model select conversations containing a matching request. The
    # latest status and expanded details always come from the full conversation.
    if filters:
        matches = select(group_key).select_from(UsageRecord).outerjoin(
            ResponseBinding, UsageRecord.request_id == ResponseBinding.response_id
        ).where(*scope, *filters)
        eligible = eligible.where(ranked.c.group_key.in_(matches))
    if status == 'error':
        eligible = eligible.where(ranked.c.status_code >= 400)
    elif status == 'success':
        eligible = eligible.where(ranked.c.status_code < 400)
    eligible = eligible.subquery()
    total = await db.scalar(select(func.count()).select_from(eligible)) or 0
    request_total = await db.scalar(select(func.count()).select_from(ranked).join(
        eligible, ranked.c.group_key == eligible.c.group_key)) or 0
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    selected = select(eligible).order_by(eligible.c.latest_at.desc(), eligible.c.group_key).offset(
        (page - 1) * page_size).limit(page_size).subquery()
    rows = (await db.execute(select(UsageRecord, ApiKey.name, Worker.name,
        ranked.c.conversation, selected.c.latest_at, selected.c.group_key, ranked.c.worker_thread)
        .select_from(UsageRecord).join(ranked, UsageRecord.id == ranked.c.id)
        .join(selected, ranked.c.group_key == selected.c.group_key)
        .outerjoin(ApiKey, UsageRecord.api_key_id == ApiKey.id)
        .outerjoin(Worker, UsageRecord.worker_id == Worker.id)
        .order_by(selected.c.latest_at.desc(), selected.c.group_key,
                  UsageRecord.created_at.asc(), UsageRecord.id.asc()))).all()
    groups = {}
    for usage, key_name, worker_name, conversation_id, latest_at, identity, worker_thread in rows:
        group = groups.setdefault(identity, {
            'identity': identity, 'thread_id': conversation_id, 'conversation_id': conversation_id,
            'logical': bool(usage.logical_conversation_id), 'thread_ids': [],
            'active_url': history_link(conversation_id, usage.api_key_id, usage.endpoint or 'unknown').replace('/admin/history?', '/admin/sessions?'), 'endpoint': usage.endpoint or 'unknown',
            'key_name': key_name or '已删除', 'latest_at': latest_at, 'requests': [],
            'input_tokens': 0, 'output_tokens': 0, 'duration_ms': 0,
            'cost_usd': Decimal(0), 'unpriced': False,
        })
        group['requests'].append({'usage': usage, 'worker_name': worker_name or '—', 'thread_id': worker_thread})
        if worker_thread and worker_thread not in group['thread_ids']:
            group['thread_ids'].append(worker_thread)
        for field in ('input_tokens', 'output_tokens', 'duration_ms'):
            group[field] += getattr(usage, field)
        group['latest_status'] = usage.status_code
        group['cost_usd'] += usage.cost_usd or Decimal(0)
        group['unpriced'] |= usage.cost_usd is None
    return {'groups': list(groups.values()), 'total': total, 'request_total': request_total,
            'page': page, 'pages': pages}


def history_link(conversation_id, key_id, endpoint):
    from urllib.parse import urlencode
    return '/admin/history?' + urlencode({'conversation': conversation_id,
        'key_id': str(key_id or 'development'), 'endpoint': endpoint})


def active_conversation_groups(rows):
    """Group live bindings by the same key/endpoint/conversation as history.

    Rows must be ordered newest binding first. Mapping comes from its exact
    response record; a missing logical ID explicitly falls back to the Thread.
    """
    groups = {}
    by_key = {}
    for binding, key, worker, logical_id, request_thread, request_endpoint in rows:
        conversation_id = logical_id or request_thread or binding.thread_id
        endpoint = request_endpoint or 'unknown'
        identity = (key.id, endpoint, conversation_id)
        if identity not in groups:
            group = {'conversation_id': conversation_id,
                'logical': bool(logical_id),
                'key': key, 'endpoint': endpoint, 'threads': [], 'binding_count': 0,
                'latest_at': binding.last_used_at,
                'history_url': history_link(conversation_id, key.id, endpoint)}
            groups[identity] = group
            by_key.setdefault(key.id, []).append(group)
        group = groups[identity]
        group['binding_count'] += 1
        thread = next((t for t in group['threads'] if t['binding'].thread_id == binding.thread_id
                       and t['worker'].id == worker.id), None)
        if thread:
            thread['binding_count'] += 1
        else:
            group['threads'].append({'binding': binding, 'worker': worker, 'binding_count': 1})
    return list(groups.values()), by_key


def history_time_filters(start='', end=''):
    """Accept explicit instants; never interpret browser wall time as server time."""
    from datetime import datetime, timezone
    from fastapi import HTTPException
    bounds = []
    for value in (start, end):
        if not value:
            bounds.append(None)
            continue
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError('timezone required')
            bounds.append(parsed.astimezone(timezone.utc))
        except (ValueError, OverflowError):
            raise HTTPException(400, '时间格式无效，请提供带时区的时间')
    start_at, end_at = bounds
    if start_at and end_at and start_at >= end_at:
        raise HTTPException(400, '结束时间必须晚于开始时间')
    filters = []
    if start_at:
        filters.append(UsageRecord.created_at >= start_at)
    if end_at:
        filters.append(UsageRecord.created_at < end_at)
    return filters
