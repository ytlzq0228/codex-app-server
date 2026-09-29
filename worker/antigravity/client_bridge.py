"""Per-turn MCP relay. Client tools are forwarded, never executed by the worker."""
import asyncio
import json
import secrets
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

TOOL_TTL = 300
ACTIVE = {}


class ToolBridge:
    def __init__(self, tools, images=None):
        self.token = secrets.token_urlsafe(32)
        self.server_name = "gateway_client_" + uuid4().hex[:16]
        self.tools = {tool["name"]: tool for tool in tools}
        if len(self.tools) != len(tools) or len(tools) > 64:
            raise ValueError("Invalid tool declarations")
        self.images = images or []
        self.images_read = set()
        if self.images:
            self.tools["gateway_read_image"] = {
                "name": "gateway_read_image", "description": "Inspect one user-attached image. Read every attached image before answering or calling client tools.",
                "inputSchema": {"type": "object", "properties": {"index": {"type": "integer", "minimum": 1, "maximum": len(self.images)}},
                                "required": ["index"], "additionalProperties": False},
            }
        self.events = asyncio.Queue(maxsize=64)
        self.pending = {}
        self.serial = asyncio.Lock()
        self.closed = False

    async def call(self, name, arguments):
        if name not in self.tools or not isinstance(arguments, dict):
            raise ValueError("Undeclared tool or invalid arguments")
        if name == "gateway_read_image" and self.images:
            index = arguments.get("index")
            if type(index) is not int or not 1 <= index <= len(self.images):
                raise ValueError("Invalid attachment index")
            self.images_read.add(index)
            return {"content": [{"type": "image", **self.images[index - 1]}]}
        if len(self.images_read) != len(self.images):
            return {"isError": True, "content": [{"type": "text", "text": "Read every attached image with gateway_read_image before calling client tools."}]}
        # Match the gateway's one outstanding result per execution protocol.
        async with self.serial:
            if self.closed:
                raise ValueError("Execution closed")
            call_id = uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self.pending[call_id] = future
            try:
                await self.events.put({"event": "client_tool", "run_id": self.token,
                                       "worker_call_id": call_id, "tool": name, "arguments": arguments})
                return await asyncio.wait_for(future, TOOL_TTL)
            finally:
                self.pending.pop(call_id, None)

    def resolve(self, call_id, result):
        future = self.pending.get(call_id)
        if future is None or future.done():
            raise HTTPException(409, "Tool call unavailable or already completed")
        future.set_result(result)

    def close(self):
        self.closed = True
        ACTIVE.pop(self.token, None)
        for future in self.pending.values():
            if not future.done():
                future.cancel()

    async def messages(self, stdout):
        async def read():
            try:
                while line := await stdout.readline():
                    await self.events.put(json.loads(line))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.events.put(exc)
            await self.events.put(None)

        reader = asyncio.create_task(read())
        # Bound active inference time; client result waits have their own TTL.
        remaining = 300.0
        loop = asyncio.get_running_loop()
        try:
            while True:
                start = loop.time()
                try:
                    event = await asyncio.wait_for(self.events.get(), min(15, remaining))
                except asyncio.TimeoutError:
                    event = {"event": "heartbeat"}
                if not self.pending:
                    remaining -= loop.time() - start
                if remaining <= 0:
                    raise TimeoutError("Gemini execution timed out")
                if event is None:
                    return
                if isinstance(event, Exception):
                    raise event
                yield event
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    @contextmanager
    def configuration(self, workspace, home=None):
        home = Path.home() if home is None else Path(home)
        saved = {}
        def write(path, value):
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
                raise ValueError("Symlink configuration is not supported")
            saved[path] = path.read_bytes() if path.exists() else None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        try:
            settings_path = home / ".gemini/antigravity-cli/settings.json"
            settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
            settings.update({
                "toolPermission": "request-review", "allowNonWorkspaceAccess": False,
                "enableTelemetry": False,
                "permissions": {
                    "deny": ["read_file(*)", "write_file(*)", "command(*)", "unsandboxed(*)",
                             "read_url(*)", "execute_url(*)"],
                    "ask": [],
                    "allow": [f"mcp({self.server_name}/*)"],
                },
            })
            write(settings_path, settings)
            # Only the server-owned relay is available to this execution.
            write(home / ".gemini/config/mcp_config.json", {"mcpServers": {}})
            write(Path(workspace) / ".agents/mcp_config.json", {"mcpServers": {
                self.server_name: {"serverUrl": "http://127.0.0.1:4500/client-mcp/" + self.token}
            }})
            ACTIVE[self.token] = self
            yield
        finally:
            self.close()
            for path, value in reversed(list(saved.items())):
                if value is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(value)


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
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "gateway-client-tools", "version": "1"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": list(bridge.tools.values())}
            elif method == "tools/call":
                result = await bridge.call(params.get("name"), params.get("arguments", {}))
            else:
                return JSONResponse({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}})
            return JSONResponse({"jsonrpc": "2.0", "id": ident, "result": result})
        except (ValueError, TimeoutError):
            return JSONResponse({"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "Tool call invalid, expired or unavailable"}})


@contextmanager
def execution_environment(workspace, home=None):
    """Keep each process's settings/MCP and working directory independent."""
    home = Path.home() if home is None else Path(home)
    source = home / ".gemini/antigravity-cli"
    with tempfile.TemporaryDirectory(prefix="gateway-home-") as temporary, \
         tempfile.TemporaryDirectory(prefix=".gateway-turn-", dir=workspace) as working:
        isolated = Path(temporary)
        target = isolated / ".gemini/antigravity-cli"
        target.mkdir(parents=True)
        for name in ("settings.json", "jetski_state.pbtxt", "installation_id", "antigravity-oauth-token"):
            if (source / name).is_file():
                shutil.copy2(source / name, target / name)
        if (source / "cache").is_dir():
            shutil.copytree(source / "cache", target / "cache")
        projects = home / ".gemini/config/projects"
        if projects.is_dir():
            shutil.copytree(projects, isolated / ".gemini/config/projects")
        # Conversations are keyed by UUID. Admission rejects concurrent use of
        # the same conversation; new conversations can safely coexist.
        for name in ("conversations", "log"):
            (source / name).mkdir(parents=True, exist_ok=True)
            (target / name).symlink_to(source / name, target_is_directory=True)
        yield isolated, Path(working)
