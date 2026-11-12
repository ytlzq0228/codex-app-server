"""Client tool protocol validation. Tool definitions are data, never executable code."""
import json
import re
from uuid import uuid4
from .multimodal import content_parts

NAME = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
FORBIDDEN = {'config', 'cwd', 'environments', 'permissions', 'sandbox', 'sandboxPolicy',
             'approvalPolicy', 'approval_policy', 'dynamicTools', 'modelProvider', 'model_provider'}


class ToolProtocolError(ValueError):
    def __init__(self, message, code="invalid_client_tool"):
        super().__init__(message)
        self.code = code


def definitions(request):
    from .web_search import configuration
    configuration(request)
    tools = list(request.tools or [])
    for item in request.input if isinstance(request.input, list) else [request.input]:
        if isinstance(item, dict) and item.get('type') == 'additional_tools':
            if not isinstance(item.get('tools'), list):
                raise ToolProtocolError('additional_tools.tools must be an array')
            if any(isinstance(t, dict) and t.get('type') == 'web_search' for t in item['tools']):
                raise ToolProtocolError('Declare web_search in top-level tools')
            tools.extend(item['tools'])
    result = []
    seen = set()
    def add(tool, namespace=None):
        if not isinstance(tool, dict):
            raise ToolProtocolError('Tool definitions must be objects')
        name, kind = tool.get('name'), tool.get('type')
        if kind == 'web_search':
            if namespace:
                raise ToolProtocolError('Declare web_search in top-level tools')
            return
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
        from .grammar_tools import grammar_format
        grammar = grammar_format(tool.get('format')) if kind == 'custom' else None
        schema = tool.get('parameters', {'type':'object','properties':{}}) if kind == 'function' else {'type':'object','properties':{'input':{'type':'string'}},'required':['input'],'additionalProperties':False}
        if not isinstance(schema, dict) or schema.get('type') != 'object':
            raise ToolProtocolError('Function parameters must be an object JSON Schema')
        identity=(namespace,name)
        if identity in seen:
            raise ToolProtocolError(f'Duplicate client tool: {namespace or ""}.{name}')
        seen.add(identity)
        result.append({'alias':f'gateway_client_{len(result)}','name':name,'namespace':namespace,
                       'kind':kind,'description':str(tool.get('description') or ''),'schema':schema, 'grammar':grammar})
        if len(result)>512:
            raise ToolProtocolError('At most 512 client tools are supported')
    for tool in tools:
        add(tool)
    return result



def compatible_definitions(request, original):
    """Order and display descriptions do not change a registered tool contract.

    The suspended run keeps its original alias mapping. New/removed tools,
    schemas, kinds and grammars still require a fresh execution.
    """
    def contracts(body):
        return {(s["namespace"], s["name"]):
                {k: v for k, v in s.items() if k not in {"alias", "description"}}
                for s in definitions(body)}
    return contracts(request) == contracts(original)

def tool_outputs(request):
    items = request.input if isinstance(request.input,list) else [request.input]
    # Clients append system/developer reminders after tool results, including
    # Codex image_resize_notice messages after image outputs.
    # Preserve those reminders in the relay reply while retaining the original
    # request/history for audit and prefix verification. A new user message is
    # deliberately not consumed as a tool result.
    reminders = []
    items = list(items)
    while items and isinstance(items[-1], dict) and items[-1].get("role") in {"system", "developer"}:
        reminder = items.pop()
        reminders.insert(0, request._item_text(reminder))
    outputs = []
    for item in reversed(items):
        if not isinstance(item,dict) or item.get('type') not in {'function_call_output','custom_tool_call_output'}:
            break
        call_id = item.get('call_id')
        output = item.get('output')
        if not isinstance(call_id,str) or not call_id:
            raise ToolProtocolError('Tool output requires a call_id')
        try:
            parts = content_parts(output)
        except ValueError as exc:
            raise ToolProtocolError(str(exc)) from exc
        if isinstance(output, list):
            output = parts
        outputs.append((call_id,output))
    if len({i for i,_ in outputs})!=len(outputs):
        raise ToolProtocolError('Duplicate tool output call_id')
    outputs = list(reversed(outputs))
    if outputs and reminders:
        call_id, output = outputs[-1]
        extra = "\n\n".join(reminders)
        output = output + "\n\n" + extra if isinstance(output, str) else [*output, {"type": "input_text", "text": extra}]
        outputs[-1] = (call_id, output)
    return outputs


def terminal_tool_call(request, call):
    """An explicit one-shot contract, or the narrow StructuredOutput compatibility policy."""
    from .config import get_settings
    mode = (request.metadata or {}).get('gateway_tool_lifecycle')
    if mode == 'interactive' or request.tool_choice == 'none':
        return False
    specs = definitions(request)
    if len(specs) != 1:
        return False
    spec = specs[0]
    if spec['kind'] != 'function' or spec['namespace'] or call.get('name') != spec['name']:
        return False
    if mode != 'terminal' and not (get_settings().structured_output_tool_terminal and
                                    spec['name'] == 'StructuredOutput'):
        return False
    # Never finish on malformed generated data, including providers without native schema enforcement.
    from jsonschema.validators import validator_for
    from jsonschema.exceptions import ValidationError, SchemaError
    try:
        validator_for(spec['schema'])(spec['schema']).validate(json.loads(call['arguments']))
    except (ValueError, KeyError, TypeError, ValidationError, SchemaError) as exc:
        raise ToolProtocolError('Terminal tool arguments did not match their JSON Schema',
                                'structured_output_invalid') from exc
    return True


def result_before_user_turn(request):
    """A single adjacent result followed by a new user message, without losing roles.

    This is only a candidate for authenticated cancellation, never an RPC reply.
    Require the matching call and earlier user context before replacing a turn.
    """
    if request.previous_response_id or not isinstance(request.input, list):
        return None
    items = request.input
    end = len(items)
    while end and isinstance(items[end-1], dict) and items[end-1].get('role') in {'system', 'developer'}:
        end -= 1
    if not end or not isinstance(items[end-1], dict) or items[end-1].get('role') != 'user':
        return None
    end -= 1
    if not end:
        return None
    candidate = request.model_copy(update={'input': items[:end]})
    outputs = tool_outputs(candidate)
    if not outputs:
        return None
    if len(outputs) != 1:
        raise ToolProtocolError('Return exactly one pending tool result before a new user turn')
    call_id = outputs[0][0]
    calls = [i for i in items[:end] if isinstance(i, dict) and
             i.get('type') in {'function_call', 'custom_tool_call'} and i.get('call_id') == call_id]
    results = [i for i in items if isinstance(i, dict) and
               i.get('type') in {'function_call_output', 'custom_tool_call_output'} and i.get('call_id') == call_id]
    if len(calls) != 1 or len(results) != 1 or not any(
            isinstance(i, dict) and i.get('role') == 'user' for i in items[:end]):
        raise ToolProtocolError('A new user turn after a tool result requires unambiguous full history')
    if items.index(calls[0]) >= items.index(results[0]):
        raise ToolProtocolError('Tool call must precede its result')
    return candidate


def validate(request):
    for name in FORBIDDEN:
        if (request.model_extra or {}).get(name) is not None:
            raise ToolProtocolError(f'{name} cannot override the Worker security policy')
    specs=definitions(request)
    lifecycle = (request.metadata or {}).get('gateway_tool_lifecycle')
    if lifecycle not in {None, 'interactive', 'terminal'}:
        raise ToolProtocolError('metadata.gateway_tool_lifecycle must be terminal or interactive')
    if lifecycle == 'terminal' and (len(specs) != 1 or specs[0]['kind'] != 'function' or specs[0]['namespace']):
        raise ToolProtocolError('Terminal output requires exactly one non-namespaced function tool')
    # Validate historical outputs as well as the pending output at the tail.
    for item in request.input if isinstance(request.input, list) else [request.input]:
        if isinstance(item, dict) and item.get('type') in {'function_call_output', 'custom_tool_call_output'}:
            try:
                content_parts(item.get('output'))
            except ValueError as exc:
                raise ToolProtocolError(str(exc)) from exc
    outputs = tool_outputs(request)
    if request.previous_response_id and specs and not outputs:
        raise ToolProtocolError("Client tool requests must send full history or return a pending call_id; tool definitions cannot be attached to a resumed Worker thread")
    forced_specs(request, specs)
    return specs


def dynamic_specs(specs):
    result = []
    for spec in specs:
        description = f"Client-side tool {spec['namespace'] or ''}.{spec['name']}. {spec['description']}"
        if grammar := spec.get('grammar'):
            description += (
                "\nReturn the raw tool input in the JSON input string, without Markdown fences. "
                "It must fully match this client-provided " + grammar['syntax'] + " grammar:\n"
                + grammar['definition']
            )
        result.append({'type':'function', 'name':spec['alias'], 'description':description,
                       'inputSchema':spec['schema']})
    return result


def public_call(specs, params):
    spec=next((s for s in specs if s['alias']==params.get('tool')),None)
    if spec is None or params.get('namespace'):
        raise ToolProtocolError('Worker requested an undeclared client tool')
    args=params.get('arguments')
    if not isinstance(args,dict):
        raise ToolProtocolError('Worker returned invalid client tool arguments')
    call={'id':('fc_' if spec['kind']=='function' else 'ctc_')+uuid4().hex,'type':'function_call' if spec['kind']=='function' else 'custom_tool_call',
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


def forced_specs(request, specs):
    """Validate forced selection and return the allowed tools, or None for auto."""
    choice = request.tool_choice
    if choice in (None, "auto", "none"):
        return None
    from .providers import provider_for
    if provider_for(request.model) != "claude":
        raise ToolProtocolError("Forced tool choice is only supported for Claude")
    if choice == "required":
        selected = specs
    elif isinstance(choice, dict) and choice.get("type") == "function" and not set(choice) - {"type", "name", "namespace"}:
        selected = [s for s in specs if s["name"] == choice.get("name")
                    and s["namespace"] == choice.get("namespace") and s["kind"] == "function"]
    else:
        raise ToolProtocolError("Invalid forced tool choice")
    if not selected:
        raise ToolProtocolError("Forced tool choice requires a declared matching tool")
    return selected
