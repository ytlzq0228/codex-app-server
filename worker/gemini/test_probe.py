"""Account-free checks for the ACP harness and the deterministic MCP server."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import unittest

from probe import Client, RpcError


class Writer:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(json.loads(data))

    async def drain(self):
        pass


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = Client()
        self.reader = asyncio.StreamReader()
        self.writer = Writer()
        self.client.proc = type('Process', (), {'stdout': self.reader, 'stdin': self.writer})()
        self.task = asyncio.create_task(self.client.read())

    async def asyncTearDown(self):
        self.reader.feed_eof()
        await self.task

    def feed(self, message):
        self.reader.feed_data((json.dumps(message) + '\n').encode())

    async def test_interleaved_events_and_replies(self):
        first = asyncio.create_task(self.client.call('first', {}))
        second = asyncio.create_task(self.client.call('second', {}))
        await asyncio.sleep(0)
        self.feed({'id': 2, 'result': {'value': 2}})
        self.feed({'method': 'session/update', 'params': {'sessionId': 'test'}})
        self.feed({'id': 1, 'result': {'value': 1}})
        self.assertEqual(await first, {'value': 1})
        self.assertEqual(await second, {'value': 2})
        self.assertEqual(len(self.client.events), 1)

    async def test_permission_is_denied(self):
        self.feed({'id': 90, 'method': 'session/request_permission', 'params': {}})
        await asyncio.sleep(0)
        self.assertEqual(self.writer.frames[-1]['result']['outcome']['outcome'], 'cancelled')

    async def test_rpc_errors_do_not_print_credentials(self):
        pending = asyncio.create_task(self.client.call('session/new', {}))
        await asyncio.sleep(0)
        self.feed({'id': 1, 'error': {'code': -32000, 'message': 'sensitive-upstream-content'}})
        with self.assertRaises(RpcError) as caught:
            await pending
        self.assertNotIn('sensitive', str(caught.exception))
        self.assertEqual(caught.exception.code, -32000)

    async def test_eof_unblocks_pending_request(self):
        pending = asyncio.create_task(self.client.call('session/prompt', {}))
        await asyncio.sleep(0)
        self.reader.feed_eof()
        with self.assertRaisesRegex(RuntimeError, 'exited'):
            await pending


class McpTests(unittest.TestCase):
    def test_initialize_discover_execute_and_reject(self):
        messages = [
            {'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
            {'method': 'notifications/initialized'},
            {'id': 2, 'method': 'tools/list'},
            {'id': 3, 'method': 'tools/call', 'params': {'name': 'gateway_echo', 'arguments': {'token': 'test'}}},
            {'id': 4, 'method': 'tools/call', 'params': {'name': 'run_shell_command'}},
        ]
        proc = subprocess.run([sys.executable, str(Path(__file__).with_name('mcp_probe.py'))],
                              input=''.join(json.dumps(m) + '\n' for m in messages),
                              text=True, capture_output=True, check=True, timeout=5)
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([r['id'] for r in replies], [1, 2, 3, 4])
        self.assertEqual(replies[1]['result']['tools'][0]['name'], 'gateway_echo')
        self.assertEqual(replies[2]['result']['content'][0]['text'], 'verified:test')
        self.assertEqual(replies[3]['error']['code'], -32601)


if __name__ == '__main__':
    unittest.main()
