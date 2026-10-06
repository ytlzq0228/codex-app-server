"""Per-turn MCP relay for the Claude worker. Client tools are forwarded, never executed here.

Claude Code connects to this relay over Streamable HTTP (`--mcp-config`) and sees each
client tool as `mcp__client__<name>`. Tool names keep their client spelling; the
gateway encodes namespaces before declaring them.
"""
import asyncio
import json
import secrets
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

TOOL_TTL = 300
INFERENCE_IDLE_TIMEOUT = 300.0
INFERENCE_STAGE_TIMEOUT = 1800.0
EXECUTION_TIMEOUT = 14400.0
SERVER_NAME = "client"
ACTIVE = {}


class ToolBridge:
    def __init__(self, tools):
        self.token = secrets.token_urlsafe(32)
        self.tools = {tool["name"]: tool for tool in tools}
        if len(self.tools) != len(tools) or len(tools) > 64:
            raise ValueError("Invalid tool declarations")
        self.events = asyncio.Queue(maxsize=64)
        self.pending = {}
        # One outstanding client call at a time matches the gateway continuation protocol.
        self.serial = asyncio.Lock()
        self.closed = False
        self.stage_started = self.last_progress = None

    def mcp_config(self):
        return json.dumps({"mcpServers": {SERVER_NAME: {
            "type": "http", "url": "http://127.0.0.1:4500/client-mcp/" + self.token}}})

    async def call(self, name, arguments, meta):
        if name not in self.tools or not isinstance(arguments, dict):
            raise ValueError("Undeclared tool or invalid arguments")
        async with self.serial:
            if self.closed:
                raise ValueError("Execution closed")
            call_id = uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self.pending[call_id] = future
            try:
                await self.events.put({"event": "client_tool", "run_id": self.token, "worker_call_id": call_id,
                                       "tool": name, "arguments": arguments,
                                       "tool_use_id": (meta or {}).get("claudecode/toolUseId")})
                return await asyncio.wait_for(future, TOOL_TTL)
            finally:
                self.pending.pop(call_id, None)

    def resolve(self, call_id, result):
        future = self.pending.get(call_id)
        if future is None or future.done():
            raise HTTPException(409, "Tool call unavailable or already completed")
        self.stage_started = self.last_progress = asyncio.get_running_loop().time()
        future.set_result(result)

    def close(self):
        self.closed = True
        ACTIVE.pop(self.token, None)
        for future in self.pending.values():
            if not future.done():
                future.cancel()

    async def messages(self, stdout):
        """Merge CLI stdout lines with relay events; heartbeat while idle."""
        async def read():
            try:
                while line := await stdout.readline():
                    try:
                        await self.events.put(json.loads(line))
                    except ValueError:
                        continue  # Non-JSON diagnostics never reach the gateway.
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.events.put(exc)
            await self.events.put(None)

        reader = asyncio.create_task(read())
        loop = asyncio.get_running_loop()
        started = self.stage_started = self.last_progress = loop.time()
        try:
            while True:
                now = loop.time()
                deadlines = [started + EXECUTION_TIMEOUT]
                if not self.pending:
                    deadlines += [self.last_progress + INFERENCE_IDLE_TIMEOUT,
                                  self.stage_started + INFERENCE_STAGE_TIMEOUT]
                remaining = min(deadlines) - now
                if remaining <= 0:
                    raise TimeoutError("Claude execution timed out")
                try:
                    event = await asyncio.wait_for(self.events.get(), min(15, remaining))
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat"}
                    continue
                if isinstance(event, dict) and (
                    event.get("type") in {"assistant", "result"}
                    or event.get("event") == "client_tool"
                    or (event.get("type") == "stream_event"
                        and isinstance(event.get("event"), dict)
                        and event["event"].get("type") == "content_block_delta")
                ):
                    self.last_progress = loop.time()
                if event is None:
                    return
                if isinstance(event, Exception):
                    raise event
                yield event
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


def install_mcp(app):
    @app.post("/client-mcp/{token}")
    async def mcp(token: str, request: Request):
        bridge = ACTIVE.get(token)
        if bridge is None or bridge.closed:
            raise HTTPException(404, "Execution unavailable")
        raw = await request.body()
        if len(raw) > 2**20:
            raise HTTPException(413, "MCP message too large")
        try:
            message = json.loads(raw)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError()
        except (ValueError, TypeError):
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC message"}})
        method, ident = message.get("method"), message.get("id")
        if ident is None:
            return Response(status_code=202)
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return JSONResponse({"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "Invalid params"}})
        try:
            if method == "initialize":
                result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "gateway-client-tools", "version": "1"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": list(bridge.tools.values())}
            elif method == "tools/call":
                result = await bridge.call(params.get("name"), params.get("arguments", {}), params.get("_meta"))
            else:
                # Includes the optional pre-initialize `server/discover` probe.
                return JSONResponse({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}})
            return JSONResponse({"jsonrpc": "2.0", "id": ident, "result": result})
        except (ValueError, TimeoutError):
            return JSONResponse({"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "Tool call invalid, expired or unavailable"}})
