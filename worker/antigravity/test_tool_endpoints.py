import asyncio
import unittest
from unittest.mock import patch
import service

class ToolEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_result_auth_scope_replay_and_text_only(self):
        import os
        import httpx
        bridge = service.ToolBridge([{"name": "gateway_client_0"}])
        service.ACTIVE[bridge.token] = bridge
        task = asyncio.create_task(bridge.call("gateway_client_0", {}))
        event = await bridge.events.get()
        body = {"run_id": bridge.token, "worker_call_id": event["worker_call_id"],
                "content": [{"type": "text", "text": "ok"}]}
        try:
            with patch.dict(os.environ, {"CODEX_WORKER_TOKEN": "unit-test-token"}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app), base_url="http://worker") as client:
                    self.assertEqual((await client.post("/tool-result", json=body)).status_code, 401)
                    headers = {"Authorization": "Bearer unit-test-token"}
                    caps = await client.post("/capabilities", headers=headers)
                    self.assertEqual(caps.json()["client_tools"], 1)
                    self.assertEqual((await client.post("/tool-result", headers=headers,
                        json={**body, "run_id": "wrong-run"})).status_code, 409)
                    self.assertEqual((await client.post("/tool-result", headers=headers,
                        json={**body, "content": [{"type": "image", "data": "x"}]})).status_code, 400)
                    self.assertEqual((await client.post("/tool-result", headers=headers, json=body)).status_code, 200)
                    self.assertEqual((await client.post("/tool-result", headers=headers, json=body)).status_code, 409)
            self.assertEqual((await task)["content"], body["content"])
        finally:
            bridge.close()
            await asyncio.gather(task, return_exceptions=True)
