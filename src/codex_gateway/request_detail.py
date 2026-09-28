"""Readable projections of stored request data; never changes forwarding data."""

LABELS = {
    'method': '关联方式', 'version': '版本', 'client_thread_ids': '客户端 Thread',
    'installation_ids': '安装标识', 'client_sources': '客户端来源',
    'thread_sources': 'Thread 来源', 'turn_ids': '轮次标识', 'session_ids': '会话标识',
    'auto_resume': '自动续用', 'category': '会话类型', 'parent_request_id': '父请求',
    'execution': '执行决策', 'execution_outcome': '执行结果', 'rejection': '拒绝原因',
    'history_observation': '历史匹配观测', 'mode': '模式', 'result': '结果',
    'candidate_request_ids': '候选请求', 'action': '动作', 'reason': '原因',
    'output_sha256': '输出摘要', 'client_tool_call_ids': '客户端工具调用',
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
        label = prefix + LABELS.get(key, key)
        if isinstance(item, dict):
            rows.extend(readable_fields(item, label + ' · '))
            continue
        def display(part):
            if part is None or part == '':
                return '未记录'
            if isinstance(part, bool):
                return '是' if part else '否'
            return VALUES.get(str(part), str(part))
        rows.append((label, '、'.join(display(part) for part in item) or '未记录'
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
    return [(label, item if item is not None and item != '' else '未记录') for label, item in [
        ('Client IP', value.get('client_ip')), ('Client Address', address),
        ('User-Agent', ' / '.join(agents)), ('Path', value.get('path')),
        ('HTTP 方法', value.get('method')), ('协议', value.get('scheme')),
        ('HTTP 版本', value.get('http_version')), ('接收时间', value.get('received_at')),
        ('网关请求 ID', value.get('gateway_request_id')),
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
        elif isinstance(content, list) and role not in ('system', 'developer'):
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
