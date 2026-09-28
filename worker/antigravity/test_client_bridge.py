import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, HTTPException
import httpx
import client_bridge
from client_bridge import ToolBridge, ACTIVE, install_mcp


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_relay_serial_result_and_replay(self):
        bridge = ToolBridge([{"name": "gateway_client_0", "inputSchema": {"type": "object"}}])
        task = asyncio.create_task(bridge.call("gateway_client_0", {"x": 1}))
        event = await bridge.events.get()
        result = {"content": [{"type": "text", "text": "client result"}]}
        bridge.resolve(event["worker_call_id"], result)
        self.assertEqual(await task, result)
        with self.assertRaises(HTTPException):
            bridge.resolve(event["worker_call_id"], result)
        with self.assertRaises(ValueError):
            await bridge.call("run_command", {})
        bridge.close()

    async def test_mcp_scope_and_protocol(self):
        app = FastAPI()
        install_mcp(app)
        bridge = ToolBridge([{"name": "gateway_client_0", "inputSchema": {"type": "object"}}])
        ACTIVE[bridge.token] = bridge
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                async def rpc(method, params=None, token=None):
                    return await client.post("/client-mcp/" + (token or bridge.token),
                        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
                self.assertEqual((await rpc("initialize")).json()["result"]["capabilities"], {"tools": {}})
                self.assertEqual(len((await rpc("tools/list")).json()["result"]["tools"]), 1)
                self.assertEqual((await rpc("tools/list", token="wrong")).status_code, 404)
                self.assertIn("error", (await rpc("tools/call", {"name": "shell"})).json())
                pending = asyncio.create_task(rpc("tools/call", {"name": "gateway_client_0", "arguments": {}}))
                event = await bridge.events.get()
                bridge.resolve(event["worker_call_id"], {"content": [{"type": "text", "text": "ok"}]})
                self.assertEqual((await pending).json()["result"]["content"][0]["text"], "ok")
        finally:
            bridge.close()
        self.assertNotIn(bridge.token, ACTIVE)

    async def test_expiry_and_cancel_release_pending(self):
        bridge = ToolBridge([{"name": "gateway_client_0"}])
        from unittest.mock import patch
        with patch.object(client_bridge, "TOOL_TTL", 0.01):
            with self.assertRaises(TimeoutError):
                await bridge.call("gateway_client_0", {})
        self.assertFalse(bridge.pending)
        task = asyncio.create_task(bridge.call("gateway_client_0", {}))
        await asyncio.sleep(0)
        bridge.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(bridge.pending)

    async def test_configuration_restores_and_blocks_native_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            workspace = Path(directory) / "workspace"
            settings = home / ".gemini/antigravity-cli/settings.json"
            settings.parent.mkdir(parents=True)
            original = b'{"useG1Credits": false, "permissions":{"allow":["command(*)"]}}'
            settings.write_bytes(original)
            global_mcp = home / ".gemini/config/mcp_config.json"
            global_mcp.parent.mkdir(parents=True)
            global_mcp.write_text('{"mcpServers":{"unrelated":{}}}')
            bridge = ToolBridge([])
            with self.assertRaises(RuntimeError):
                with bridge.configuration(workspace, home):
                    policy = json.loads(settings.read_text())["permissions"]
                    self.assertIn("command(*)", policy["deny"])
                    self.assertIn("read_file(*)", policy["deny"])
                    self.assertIn("write_file(*)", policy["deny"])
                    self.assertEqual(policy["allow"], [f"mcp({bridge.server_name}/*)"])
                    self.assertEqual(json.loads(global_mcp.read_text()), {"mcpServers": {}})
                    self.assertIn(bridge.token, (workspace / ".agents/mcp_config.json").read_text())
                    raise RuntimeError("simulated cancellation")
            self.assertEqual(settings.read_bytes(), original)
            self.assertIn("unrelated", global_mcp.read_text())
            self.assertFalse((workspace / ".agents/mcp_config.json").exists())
            self.assertNotIn(bridge.token, ACTIVE)

    async def test_malformed_stdout_closes_reader(self):
        bridge = ToolBridge([])
        stream = asyncio.StreamReader()
        stream.feed_data(b"invalid-json\n")
        stream.feed_eof()
        with self.assertRaises(ValueError):
            async for _ in bridge.messages(stream):
                pass
        bridge.close()
