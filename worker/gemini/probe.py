"""Isolated ACP probe. Never reads or prints OAuth credentials.

Default: account-free initialization. --authenticated: after Google login.
Proves ACP/MCP behavior, not OpenAI tool-call compatibility.
"""
import argparse
import asyncio
import json
import os
import signal
import tempfile
from pathlib import Path
import uuid


class RpcError(RuntimeError):
    def __init__(self, error):
        self.code = error.get('code')
        super().__init__('ACP request failed (code=%s)' % self.code)


class Client:
    def __init__(self, environment=None):
        self.environment = environment
        self.pending = {}
        self.events = []
        self.sequence = 0
        self.proc = None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            'gemini', '--acp', stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=self.environment, start_new_session=True)
        self.reader = asyncio.create_task(self.read())
        return await self.call('initialize', {'protocolVersion': 1,
            'clientInfo': {'name': 'gateway-probe', 'version': '1'}, 'clientCapabilities': {}})

    async def send(self, payload):
        self.proc.stdin.write((json.dumps({'jsonrpc': '2.0', **payload}) + '\n').encode())
        await self.proc.stdin.drain()

    async def read(self):
        try:
            while line := await self.proc.stdout.readline():
                event = json.loads(line)
                if 'method' in event:
                    self.events.append(event)
                    if 'id' in event:
                        if event['method'] == 'session/request_permission':
                            await self.send({'id': event['id'], 'result': {'outcome': {'outcome': 'cancelled'}}})
                        else:
                            await self.send({'id': event['id'], 'error': {'code': -32601, 'message': 'Unsupported'}})
                elif event.get('id') in self.pending:
                    future = self.pending[event['id']]
                    if not future.done():
                        if 'error' in event:
                            future.set_exception(RpcError(event['error']))
                        else:
                            future.set_result(event.get('result', {}))
        except Exception as exc:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(exc)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError('ACP process exited'))

    async def call(self, method, params, timeout=120):
        self.sequence += 1
        seq = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[seq] = future
        try:
            await self.send({'id': seq, 'method': method, 'params': params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(seq, None)

    async def prompt(self, session, text):
        self.events.clear()
        result = await self.call('session/prompt', {'sessionId': session,
            'prompt': [{'type': 'text', 'text': text}]}, timeout=180)
        updates = [e.get('params', {}).get('update', {}) for e in self.events]
        chunks = [u.get('content', {}).get('text', '') for u in updates
                  if u.get('sessionUpdate') == 'agent_message_chunk']
        return result, ''.join(chunks), updates

    async def close(self):
        if not self.proc:
            return
        # The CLI launcher can spawn a child. Terminate the entire process group.
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(asyncio.gather(self.proc.wait(), self.reader), 5)
        except asyncio.TimeoutError:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.proc.wait()



def report(check, status, **details):
    print(json.dumps({'check': check, 'status': status, **details}), flush=True)


async def main(authenticated):
    if authenticated:
        settings = Path.home() / '.gemini/settings.json'
        selected = json.loads(settings.read_text()).get('security', {}).get('auth', {}).get('selectedType')
        if selected != 'oauth-personal':
            raise RuntimeError('Google subscription login required; API-key authentication is not accepted')
        if not (Path.home() / '.gemini/oauth_creds.json').is_file():
            raise RuntimeError('Complete Google login before authenticated checks')
    temporary = tempfile.TemporaryDirectory(prefix='gemini-acp-init-') if not authenticated else None
    environment = {**os.environ, 'HOME': temporary.name} if temporary else None
    client = Client(environment)
    try:
        init = await client.start()
        report('initialize', 'pass', agent=init.get('agentInfo'), capabilities=init.get('agentCapabilities'),
               auth_methods=[m['id'] for m in init.get('authMethods', [])])
        if not authenticated:
            return
        cwd = '/workspace/probe-' + uuid.uuid4().hex
        os.mkdir(cwd, 0o700)
        mcp = []  # The sole trusted MCP server is fixed in the image settings.
        created = await client.call('session/new', {'cwd': cwd, 'mcpServers': mcp})
        sid = created['sessionId']
        report('google_login', 'pass', model=created.get('models', {}).get('currentModelId'),
               available_models=[m['modelId'] for m in created.get('models', {}).get('availableModels', [])])
        token = uuid.uuid4().hex
        result, text, updates = await client.prompt(sid, 'Remember this token for this conversation: ' + token + '. Reply with it.')
        assert result['stopReason'] == 'end_turn' and token in text
        assert any(u.get('sessionUpdate') == 'agent_message_chunk' for u in updates)
        report('text_stream', 'pass')
        _, text, _ = await client.prompt(sid, 'What exact token did I ask you to remember?')
        assert token in text
        report('conversation', 'pass')
        _, text, updates = await client.prompt(sid, 'Call gateway_echo with the remembered token, then give its exact returned text.')
        executed = [u for u in updates if u.get('sessionUpdate') == 'tool_call_update' and u.get('status') == 'completed']
        assert executed and 'verified:' + token in text
        report('mcp_tool_roundtrip', 'pass')
        await client.close()
        client = Client()
        await client.start()
        await client.call('session/load', {'sessionId': sid, 'cwd': cwd, 'mcpServers': mcp})
        _, text, _ = await client.prompt(sid, 'What exact token did I ask you to remember?')
        assert token in text
        report('session_restore', 'pass')
        pending = asyncio.create_task(client.prompt(sid, 'Write a very long story, at least 10000 words.'))
        async with asyncio.timeout(30):
            while not pending.done() and not any(
                e.get('params', {}).get('update', {}).get('sessionUpdate') == 'agent_message_chunk'
                for e in client.events
            ):
                await asyncio.sleep(0.05)
        if pending.done():
            raise RuntimeError('Prompt finished before cancellation could be verified')
        await client.send({'method': 'session/cancel', 'params': {'sessionId': sid}})
        result, _, _ = await asyncio.wait_for(pending, 30)
        assert result['stopReason'] == 'cancelled'
        report('cancel', 'pass')
        report('quota_exhaustion', 'not_tested', reason='Requires an already quota-limited test account')
    finally:
        await client.close()
        if temporary:
            temporary.cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--authenticated', action='store_true')
    args = parser.parse_args()
    asyncio.run(main(args.authenticated))
