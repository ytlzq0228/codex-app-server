"""Storage-only previews; never mutate execution or audit capture inputs."""
import json


TOOL_OUTPUT_BYTES = 64


def _preview(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    # Slice characters first so even very large strings need only a tiny encode.
    prefix = text[:TOOL_OUTPUT_BYTES + 1].encode('utf-8')
    if len(prefix) <= TOOL_OUTPUT_BYTES:
        return value
    return prefix[:TOOL_OUTPUT_BYTES].decode('utf-8', errors='ignore') + '...'


def _item(item):
    if not isinstance(item, dict):
        return item
    result = item.copy()
    if item.get('type') in {'function_call_output', 'custom_tool_call_output', 'computer_call_output'}:
        if 'output' in item:
            result['output'] = _preview(item['output'])
    if item.get('role') in {'tool', 'function'} or item.get('type') == 'tool_result':
        if 'content' in item:
            result['content'] = _preview(item['content'])
    elif isinstance(item.get('content'), list):
        result['content'] = [
            _item(block) if isinstance(block, dict) and block.get('type') == 'tool_result' else block
            for block in item['content']
        ]
    return result


def stored_request_params(params):
    """Keep at most 64 UTF-8 bytes plus '...' per incoming tool result."""
    if not isinstance(params, dict):
        return params
    result = params.copy()
    for key in ('input', 'messages'):
        if isinstance(params.get(key), list):
            result[key] = [_item(item) for item in params[key]]
    return result
