"""Scoped client identity shared by audit grouping and guarded execution resume."""
import hashlib
import json
from functools import wraps
import anyio
from sqlalchemy import select
from .models import UsageRecord


def durable_write(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        # Starlette cancels the request scope when a streaming client disconnects.
        with anyio.CancelScope(shield=True):
            return await function(*args, **kwargs)
    return wrapped


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def explicit_identity(params, observation, key, endpoint, request_id):
    params = params or {}
    cm = params.get('client_metadata') or {}
    if not isinstance(cm, dict):
        cm = {}
    headers = {}
    for h in (observation or {}).get('headers', []):
        headers.setdefault(h['name'], []).append(h['value'])
    metadata = []
    for raw in [cm.get('x-codex-turn-metadata'), *headers.get('x-codex-turn-metadata', [])]:
        try:
            value = json.loads(raw) if isinstance(raw, str) else {}
            if isinstance(value, dict):
                metadata.append(value)
        except ValueError:
            pass
    def values(name, header=None):
        result = [cm.get(name), *[m.get(name) for m in metadata]]
        if header:
            result += headers.get(header, [])
        return sorted({v for v in result if isinstance(v, str) and v and len(v) <= 256
                       and not v.startswith(('[REDACTED]', '[OMITTED:', '[DEPTH LIMIT]'))})
    threads = values('thread_id', 'thread-id')
    installations = values('installation_id', 'x-codex-installation-id')
    if isinstance(cm.get('x-codex-installation-id'), str):
        installations = sorted(set(installations + [cm['x-codex-installation-id']]))
    sources = sorted(set(headers.get('originator', [])))
    kinds = values('thread_source')
    evidence = {'version': 1, 'method': 'isolated', 'client_thread_ids': threads,
                'installation_ids': installations, 'client_sources': sources,
                'thread_sources': kinds, 'turn_ids': values('turn_id'),
                'session_ids': values('session_id', 'session-id'), 'auto_resume': False}
    if any(len(v) > 1 for v in (threads, installations, sources, kinds)):
        evidence['method'] = 'identifier_conflict'
    elif threads and key is not None:
        evidence['method'] = 'explicit_client_thread'
        # Title generation must not share the conversational group.
        category = 'thread_title' if kinds == ['thread_title'] else 'conversation'
        evidence['category'] = category
        return 'conv_' + digest([str(key), endpoint, sources, installations, threads[0], category]), evidence
    return None, evidence


def chat_history(params):
    items = (params or {}).get('messages')
    if not isinstance(items, list) or not items:
        return None
    normalized = []
    for item in items:
        if not isinstance(item, dict) or set(item) - {'role', 'content'}:
            return None
        if item.get('role') not in {'system', 'developer', 'user', 'assistant'} or not isinstance(item.get('content'), str):
            return None
        normalized.append({'role': item['role'], 'content': item['content']})
    return normalized


async def correlate(db, record, output=None):
    logical, evidence = explicit_identity(record.request_params, record.request_observation,
                                         record.api_key_id, record.endpoint, record.request_id)
    if record.previous_response_id and record.api_key_id is not None:
        prior = await db.scalar(select(UsageRecord).where(UsageRecord.api_key_id == record.api_key_id,
            UsageRecord.request_id == record.previous_response_id, UsageRecord.endpoint == record.endpoint))
        if prior:
            logical = prior.logical_conversation_id or prior.thread_id or prior.request_id
            evidence.update(method='previous_response', parent_request_id=prior.request_id)
    record.logical_conversation_id = logical
    evidence["execution_outcome"] = "completed" if record.status_code == 200 and output is not None else "failed"
    record.conversation_evidence = evidence
    if output is not None:
        evidence['output_sha256'] = digest(output)
    items = chat_history(record.request_params)
    if items is not None:
        config = {k: (record.request_params or {}).get(k) for k in ('model', 'tools', 'tool_choice')}
        if items[-1]['role'] == 'user' and record.api_key_id is not None:
            fingerprint = digest([config, items[:-1]])
            matches = (await db.scalars(select(UsageRecord).where(
                UsageRecord.api_key_id == record.api_key_id, UsageRecord.endpoint == record.endpoint,
                UsageRecord.history_expected_hash == fingerprint, UsageRecord.status_code == 200
            ).order_by(UsageRecord.created_at.desc()).limit(3))).all()
            evidence['history_observation'] = {
                'mode': 'shadow', 'candidate_request_ids': [m.request_id for m in matches],
                'result': 'unique_candidate' if len(matches) == 1 else 'ambiguous' if matches else 'no_match'}
        if output is not None and record.status_code == 200:
            record.history_expected_hash = digest([config, items + [{'role': 'assistant', 'content': output}]])
