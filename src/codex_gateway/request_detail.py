"""Readable projections of stored request data; never changes forwarding data."""
from .i18n import t

LABELS = {
    'method': '关联方式', 'version': '版本', 'client_thread_ids': '客户端 Thread',
    'installation_ids': '安装标识', 'client_sources': '客户端来源',
    'thread_sources': 'Thread 来源', 'turn_ids': '轮次标识', 'session_ids': '会话标识',
    'auto_resume': '自动续用', 'category': '会话类型', 'parent_request_id': '父请求',
    'execution': '执行决策', 'execution_outcome': '执行结果', 'rejection': '拒绝原因',
    'history_observation': '历史匹配观测', 'mode': '模式', 'result': '结果',
    'candidate_request_ids': '候选请求', 'action': '动作', 'reason': '原因',
    'output_sha256': '输出摘要', 'client_tool_call_ids': '客户端工具调用',
    'usage_accounting': '用量结算', 'run_id': '执行轮次', 'final': '最终用量',
    'tokens': '原始观测用量', 'superseded_by': '已由此请求结算',
    'input_tokens': '输入 Token', 'output_tokens': '输出 Token',
    'cache_read_tokens': '缓存读取 Token', 'cache_write_tokens': '缓存写入 Token',
}
VALUES = {'isolated': '独立请求', 'identifier_conflict': '标识冲突',
          'explicit_client_thread': '客户端显式 Thread', 'previous_response': '上一条响应',
          'legacy_thread': '历史 Thread', 'completed': '已完成', 'failed': '失败',
          'waiting_client_tool': '等待客户端工具', 'resume': '续用', 'new': '新建'}


def readable_fields(value, prefix=''):
    if not isinstance(value, dict):
        return []
    rows = []
    for key, item in value.items():
        label = prefix + t(LABELS.get(key, key))
        if isinstance(item, dict):
            rows.extend(readable_fields(item, label + ' · '))
            continue
        def display(part):
            if part is None or part == '':
                return t('未记录')
            if isinstance(part, bool):
                return t('是') if part else t('否')
            return t(VALUES[str(part)]) if str(part) in VALUES else str(part)
        rows.append((label, '、'.join(display(part) for part in item) or t('未记录')
                     if isinstance(item, list) else display(item)))
    return rows


def observation_fields(value):
    if not isinstance(value, dict):
        return []
    headers = value.get('headers') or []
    agents = [str(h.get('value', '')) for h in headers
              if isinstance(h, dict) and str(h.get('name', '')).lower() == 'user-agent']
    address = value.get('client_address')
    if isinstance(address, list) and len(address) == 2:
        host, port = address
        address = f'[{host}]:{port}' if ':' in str(host) else f'{host}:{port}'
    return [(label, item if item is not None and item != '' else t('未记录')) for label, item in [
        (t('Client IP'), value.get('client_ip')), (t('Client Address'), address),
        ('User-Agent', ' / '.join(agents)), (t('Path'), value.get('path')),
        (t('HTTP 方法'), value.get('method')), (t('协议'), value.get('scheme')),
        (t('HTTP 版本'), value.get('http_version')), (t('接收时间'), value.get('received_at')),
        (t('网关请求 ID'), value.get('gateway_request_id')),
    ]]


def last_texts(params):
    """Extract the last input/output messages from Responses or Chat parameters."""
    latest = {}
    if not isinstance(params, dict):
        return latest
    items = params.get('input', params.get('messages', []))
    if isinstance(items, str):
        return {'input_text': items} if items else {}
    if not isinstance(items, list):
        return latest
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get('role')
        content = item.get('content', [item])
        kind = 'input_text' if role == 'user' else 'output_text' if role == 'assistant' else None
        if isinstance(content, str):
            if kind and content:
                latest[kind] = content
        elif isinstance(content, list) and role in (None, 'user', 'assistant') and item.get('type', 'message') in ('message', 'input_text', 'output_text'):
            parts = {}
            for part in content:
                if not isinstance(part, dict):
                    continue
                text_kind = part.get('type')
                if text_kind == 'text':
                    text_kind = kind
                if text_kind in ('input_text', 'output_text') and isinstance(part.get('text'), str) and part['text']:
                    parts.setdefault(text_kind, []).append(part['text'])
            latest.update({key: '\n\n'.join(text) for key, text in parts.items()})
    return latest


async def request_texts(db, record):
    """Use this response, never an assistant message from request history."""
    from sqlalchemy import select
    from .models import UsageRecord

    texts = last_texts(record.request_params)
    texts.pop('output_text', None)
    if record.response_text is not None:
        texts['output_text'] = record.response_text or t('本次响应未包含文本（可能为工具调用）。')
    # Responses tool continuations may carry only function_call_output. Walk
    # explicit parent links, scoped to the same owner and API key.
    prior = record
    seen = {record.request_id}
    while 'input_text' not in texts and prior.previous_response_id:
        if prior.previous_response_id in seen:
            break
        seen.add(prior.previous_response_id)
        prior = await db.scalar(select(UsageRecord).where(
            UsageRecord.request_id == prior.previous_response_id,
            UsageRecord.api_key_id == record.api_key_id,
            UsageRecord.owner_username == record.owner_username,
            UsageRecord.endpoint == record.endpoint))
        if prior is None:
            break
        value = last_texts(prior.request_params).get('input_text')
        if value:
            texts['input_text'] = value
    return texts
