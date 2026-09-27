"""Validate client grammars and generated text without executing either.

Parsing runs in bounded, disposable subprocesses outside the HTTP event loop.
The app-server bridge cannot constrain sampling natively; it validates before
exposing a custom_tool_call and can request bounded corrections from the model.
"""
import asyncio
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import sys
from weakref import WeakKeyDictionary

from .client_tools import ToolProtocolError, definitions

MAX_GRAMMAR_BYTES = 32_768
MAX_INPUT_BYTES = 262_144
TIMEOUT_SECONDS = 3
_slots = WeakKeyDictionary()
_validated = OrderedDict()


def grammar_format(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ToolProtocolError('Custom tool format must be an object')
    kind = value.get('type', 'text')
    if kind == 'text':
        return None
    if kind != 'grammar':
        raise ToolProtocolError(f'Custom tool format {kind!r} is not supported')
    syntax, source = value.get('syntax'), value.get('definition')
    if syntax not in ('lark', 'regex'):
        raise ToolProtocolError('Grammar syntax must be lark or regex')
    if not isinstance(source, str) or not source.strip():
        raise ToolProtocolError('Grammar definition must be a non-empty string')
    if len(source.encode('utf-8')) > MAX_GRAMMAR_BYTES:
        raise ToolProtocolError('Grammar definition exceeds the 32768-byte limit')
    return {'syntax': syntax, 'definition': source}


async def _check(grammar, text=None):
    loop = asyncio.get_running_loop()
    slots = _slots.setdefault(loop, asyncio.Semaphore(4))
    try:
        await asyncio.wait_for(slots.acquire(), timeout=TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise ToolProtocolError('Grammar validation is busy; retry later') from exc
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, '-I', str(Path(__file__).with_name('_grammar_worker.py')),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env={'LANG': 'C.UTF-8'},
        )
        data = json.dumps({'grammar': grammar, 'text': text}, ensure_ascii=False).encode('utf-8')
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(data), timeout=TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise ToolProtocolError('Grammar parsing exceeded the time limit') from exc
        if proc.returncode:
            raise ToolProtocolError('Grammar parser failed or exceeded its resource limit')
        result = json.loads(stdout)
        if result.get('error'):
            raise ToolProtocolError(result['error'])
        return result['matches']
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        slots.release()


async def validate_grammars(request):
    """Reject invalid declarations before sending HTTP streaming headers."""
    for spec in definitions(request):
        grammar = spec.get('grammar')
        if not grammar:
            continue
        key = hashlib.sha256(json.dumps(grammar, sort_keys=True).encode()).digest()
        if key in _validated:
            _validated.move_to_end(key)
            continue
        await _check(grammar)
        _validated[key] = True
        if len(_validated) > 128:
            _validated.popitem(last=False)


async def call_matches_grammar(specs, params, call):
    spec = next(s for s in specs if s['alias'] == params['tool'])
    grammar = spec.get('grammar')
    if not grammar:
        return True
    text = call['input']
    if len(text.encode('utf-8')) > MAX_INPUT_BYTES:
        return False
    return await _check(grammar, text)
