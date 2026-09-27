"""Client tool protocol validation. Tool definitions are data, never executable code."""
import json
import re
from uuid import uuid4

NAME = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
FORBIDDEN = {'config', 'cwd', 'environments', 'permissions', 'sandbox', 'sandboxPolicy',
             'approvalPolicy', 'approval_policy', 'dynamicTools', 'modelProvider', 'model_provider'}


class ToolProtocolError(ValueError):
    pass


def definitions(request):
    tools = list(request.tools or [])
    for item in request.input if isinstance(request.input, list) else [request.input]:
        if isinstance(item, dict) and item.get('type') == 'additional_tools':
            if not isinstance(item.get('tools'), list):
                raise ToolProtocolError('additional_tools.tools must be an array')
            tools.extend(item['tools'])
    result = []
    seen = set()
    def add(tool, namespace=None):
        if not isinstance(tool, dict):
            raise ToolProtocolError('Tool definitions must be objects')
        name, kind = tool.get('name'), tool.get('type')
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ToolProtocolError('Tool names must contain 1-64 letters, digits, underscores or hyphens')
        if kind == 'namespace':
            if namespace or not isinstance(tool.get('tools'), list):
                raise ToolProtocolError('Nested or invalid tool namespaces are not supported')
            for child in tool['tools']:
                add(child, name)
            return
        if kind not in {'function', 'custom'}:
            raise ToolProtocolError(f'Tool type {kind!r} is not supported; Worker built-in tools cannot be requested')
        if kind == 'custom' and (not isinstance(tool.get('format') or {}, dict) or (tool.get('format') or {}).get('type', 'text') != 'text'):
            raise ToolProtocolError('Custom grammar tools are not supported by this app-server bridge; use a function with JSON parameters or a custom text tool')
        schema = tool.get('parameters', {'type':'object','properties':{}}) if kind == 'function' else {'type':'object','properties':{'input':{'type':'string'}},'required':['input'],'additionalProperties':False}
        if not isinstance(schema, dict) or schema.get('type') != 'object':
            raise ToolProtocolError('Function parameters must be an object JSON Schema')
        identity=(namespace,name)
        if identity in seen:
            raise ToolProtocolError(f'Duplicate client tool: {namespace or ""}.{name}')
        seen.add(identity)
        result.append({'alias':f'gateway_client_{len(result)}','name':name,'namespace':namespace,
                       'kind':kind,'description':str(tool.get('description') or ''),'schema':schema})
        if len(result)>64:
            raise ToolProtocolError('At most 64 client tools are supported')
    for tool in tools:
        add(tool)
    return result


def tool_outputs(request):
    items = request.input if isinstance(request.input,list) else [request.input]
    outputs = []
    for item in reversed(items):
        if not isinstance(item,dict) or item.get('type') not in {'function_call_output','custom_tool_call_output'}:
            break
        call_id = item.get('call_id')
        output = item.get('output')
        if not isinstance(call_id,str) or not call_id:
            raise ToolProtocolError('Tool output requires a call_id')
        if isinstance(output,list):
            if any(not isinstance(p,dict) or p.get('type') not in {'text','input_text','output_text'} or not isinstance(p.get('text'),str) for p in output):
                raise ToolProtocolError('Only text client tool outputs are supported')
            output='\n'.join(p['text'] for p in output)
        if not isinstance(output,str):
            raise ToolProtocolError('Tool output must be text')
        outputs.append((call_id,output))
    if len({i for i,_ in outputs})!=len(outputs):
        raise ToolProtocolError('Duplicate tool output call_id')
    return list(reversed(outputs))


def validate(request):
    for name in FORBIDDEN:
        if (request.model_extra or {}).get(name) is not None:
            raise ToolProtocolError(f'{name} cannot override the Worker security policy')
    specs=definitions(request)
    outputs = tool_outputs(request)
    if request.previous_response_id and specs and not outputs:
        raise ToolProtocolError("Client tool requests must send full history or return a pending call_id; tool definitions cannot be attached to a resumed Worker thread")
    if request.tool_choice not in (None,'auto','none'):
        raise ToolProtocolError('Forced tool choice is not supported')
    return specs


def dynamic_specs(specs):
    return [{'type':'function','name':s['alias'],'description':f"Client-side tool {s['namespace'] or ''}.{s['name']}. {s['description']}",'inputSchema':s['schema']} for s in specs]


def public_call(specs, params):
    spec=next((s for s in specs if s['alias']==params.get('tool')),None)
    if spec is None or params.get('namespace'):
        raise ToolProtocolError('Worker requested an undeclared client tool')
    args=params.get('arguments')
    if not isinstance(args,dict):
        raise ToolProtocolError('Worker returned invalid client tool arguments')
    call={'id':'fc_'+uuid4().hex,'type':'function_call' if spec['kind']=='function' else 'custom_tool_call',
          'call_id':'call_'+uuid4().hex,'name':spec['name'],'status':'completed'}
    if spec['namespace']:
        call['namespace']=spec['namespace']
    if spec['kind']=='custom':
        if set(args)!={'input'} or not isinstance(args['input'],str):
            raise ToolProtocolError('Worker returned invalid custom text input')
        call['input']=args['input']
    else:
        call['arguments']=json.dumps(args,ensure_ascii=False,separators=(',',':'))
    return call
