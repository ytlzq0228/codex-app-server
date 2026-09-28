"""Single deterministic, side-effect-free MCP tool for ACP verification."""
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method = request.get('method')
    if method == 'initialize':
        result = {'protocolVersion': request['params']['protocolVersion'],
                  'capabilities': {'tools': {}}, 'serverInfo': {'name': 'gateway-probe', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'gateway_echo', 'description': 'Return a verification token unchanged.',
                  'inputSchema': {'type': 'object', 'properties': {'token': {'type': 'string'}},
                                  'required': ['token'], 'additionalProperties': False}}]}
    elif method == 'tools/call' and request.get('params', {}).get('name') == 'gateway_echo':
        result = {'content': [{'type': 'text', 'text': 'verified:' + request['params']['arguments']['token']}]}
    elif method == 'ping':
        result = {}
    else:
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32601, 'message': 'Unknown method'}}), flush=True)
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
