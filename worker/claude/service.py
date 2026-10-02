"""Private Claude worker HTTP transport. Never publish its port externally.

Runs the standalone Claude Code CLI in headless mode (`claude -p --output-format
stream-json`). Built-in tools are removed with `--tools ""`; the only tools the
model can call are the gateway's client tools, exposed through the per-turn MCP
relay in client_bridge. Each turn is one CLI process; conversations are Claude
Code sessions resumed by UUID.
"""
import asyncio
import base64
import codecs
import json
import os
import pty
import re
import signal
import tempfile
import time
from contextlib import aclosing
from pathlib import Path
from uuid import uuid4

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from client_bridge import ACTIVE, SERVER_NAME, ToolBridge, install_mcp

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
install_mcp(app)
ROOT = Path("/workspace")
SESSION_ROOT = Path.home() / ".claude" / "projects"
lock = asyncio.Lock()  # Login, account and probe runs are exclusive with turns.
login = None
active_turns = {}
MAX_TURNS = 4
rate_limit = {"info": None, "at": 0.0}

MODEL = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-\[\]]{0,119}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
EFFORT = {"low", "medium", "high", "xhigh", "max"}
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
# Permissions are enforced by the CLI flags; the settings deny list is a second fence.
SETTINGS = json.dumps({"permissions": {"defaultMode": "dontAsk", "deny": [
    "Bash", "Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep",
    "WebFetch", "WebSearch", "Agent", "Task", "TodoWrite", "KillShell", "BashOutput"]},
    "includeCoAuthoredBy": False, "enableAllProjectMcpServers": False})
BASE_ARGS = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--tools", "",
             "--strict-mcp-config", "--permission-mode", "dontAsk", "--permission-prompts", "none",
             "--disable-slash-commands", "--setting-sources", "user", "--settings", SETTINGS]


def busy():
    return lock.locked() or bool(active_turns)


def authorize(authorization: str | None = Header(default=None)):
    import hmac
    token = os.environ.get("CODEX_WORKER_TOKEN", "")
    if not token or not hmac.compare_digest(authorization or "", "Bearer " + token):
        raise HTTPException(401, "unauthorized")


def cli_env():
    return {**os.environ, "TERM": "dumb", "NO_COLOR": "1"}


def error_kind(text):
    text = (text or "").lower()
    if any(x in text for x in ("not logged in", "authentication", "oauth token", "invalid api key",
                                "401", "please run /login", "login required", "unauthorized", "token has expired")):
        return "logged_out"
    if any(x in text for x in ("rate limit", "429", "usage limit", "quota", "limit reached", "resets at", "exceeded your")):
        return "limit"
    if any(x in text for x in ("not supported", "not found", "invalid request", "400", "unsupported model", "does not exist")):
        return "request"
    return "connection"


async def stop(process):
    if process is None or process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGINT)
        await asyncio.wait_for(process.wait(), 5)
    except asyncio.TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
    except ProcessLookupError:
        await process.wait()


def remember_rate_limit(info):
    if isinstance(info, dict):
        rate_limit["info"], rate_limit["at"] = info, time.time()


def usage_payload(info):
    """Project the CLI rate_limit_event onto the Codex-shaped windows used by the UI."""
    windows = (info or {}).get("unifiedWindows") or {}
    def window(name, minutes):
        value = windows.get(name) or {}
        used = value.get("utilization")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            return None
        return {"windowDurationMins": minutes, "usedPercent": max(0, min(100, used * 100)),
                "resetsAt": value.get("resetsAt")}
    primary, secondary = window("five_hour", 300), window("seven_day", 10080)
    if primary is None and secondary is None:
        raise HTTPException(502, "Claude CLI did not report rate limit windows")
    return {"rateLimits": {k: v for k, v in (("primary", primary), ("secondary", secondary)) if v},
            "available": True, "status": (info or {}).get("status"),
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rate_limit["at"]))}


async def run_json(args, timeout=30, stdin=None):
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                cwd=str(ROOT), env=cli_env(), start_new_session=True)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(stdin), timeout)
    finally:
        await stop(proc)
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def read_account():
    code, out, err = await run_json(["claude", "auth", "status"])
    try:
        status = json.loads(out)
    except ValueError:
        return None, error_kind(out + err)
    if not status.get("loggedIn"):
        return None, "logged_out"
    return {"type": "claude-subscription", "email": status.get("email"),
            "planType": status.get("subscriptionType"), "project": status.get("orgName"),
            "authMethod": status.get("authMethod")}, None


@app.post("/capabilities", dependencies=[Depends(authorize)])
async def capabilities():
    return {"provider": "claude", "client_tools": 1, "image_input": 1, "tool_result_types": ["text", "image"],
            "native_stream": 1, "structured_output": 1, "effort": 1, "system_prompt": 1, "max_turns": MAX_TURNS}


@app.post("/account", dependencies=[Depends(authorize)])
async def account():
    if login and login.task and not login.task.done():
        raise HTTPException(409, "Login in progress")
    account, kind = await read_account()
    if account is None:
        return {"account": None, "kind": kind}
    return {"account": account, "available": True}


class ProbeInput(BaseModel):
    model: str = "claude-sonnet-5-5"


async def minimal_turn(model):
    """A real one-shot turn; auth status alone cannot detect exhausted quota."""
    if not MODEL.fullmatch(model):
        raise HTTPException(400, "Invalid model")
    args = BASE_ARGS + ["--model", model, "--no-session-persistence", "Reply with OK only."]
    code, out, err = await run_json(args, timeout=90)
    result = None
    for line in out.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("type") == "rate_limit_event":
            remember_rate_limit(item.get("rate_limit_info"))
        elif item.get("type") == "result":
            result = item
    if result is None or result.get("subtype") != "success" or result.get("is_error"):
        detail = json.dumps(result) if result else (err[-2000:] or out[-2000:])
        return False, error_kind(detail)
    return True, None


@app.post("/probe", dependencies=[Depends(authorize)])
async def probe(body: ProbeInput | None = None):
    state = await account()
    if not state.get("account"):
        return state
    if busy():
        raise HTTPException(409, "Worker busy")
    async with lock:
        ok, kind = await minimal_turn((body or ProbeInput()).model)
    if not ok:
        return {**state, "available": False, "kind": kind}
    return {**state, "rate_limits": usage_payload(rate_limit["info"]) if rate_limit["info"] else None}


@app.post("/rate-limits", dependencies=[Depends(authorize)])
async def rate_limits(body: ProbeInput | None = None):
    # Every turn refreshes the cached event; only poll with a real turn when it is stale.
    if rate_limit["info"] is None or time.time() - rate_limit["at"] > 3600:
        if busy():
            raise HTTPException(409, "Worker is busy; retry after the current operation")
        async with lock:
            ok, kind = await minimal_turn((body or ProbeInput()).model)
        if not ok and rate_limit["info"] is None:
            raise HTTPException(502, "Claude CLI did not return rate limits: " + kind)
    return usage_payload(rate_limit["info"])


IMAGE_SIGNATURES = {"image/png": lambda raw: raw.startswith(b"\x89PNG\r\n\x1a\n"),
                    "image/jpeg": lambda raw: raw.startswith(b"\xff\xd8\xff"),
                    "image/gif": lambda raw: raw.startswith((b"GIF87a", b"GIF89a")),
                    "image/webp": lambda raw: raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"}


def validate_content(content):
    images = total = 0
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("Content blocks must be objects")
        kind = block.get("type")
        if kind == "text":
            if not isinstance(block.get("text"), str) or set(block) - {"type", "text", "cache_control"}:
                raise ValueError("Invalid text block")
        elif kind == "image":
            source = block.get("source")
            if not isinstance(source, dict) or source.get("type") != "base64" or set(block) - {"type", "source", "cache_control"}:
                raise ValueError("Images must use base64 sources")
            try:
                raw = base64.b64decode(source.get("data", ""), validate=True)
            except ValueError:
                raise ValueError("Invalid image base64") from None
            check = IMAGE_SIGNATURES.get(source.get("media_type"))
            if check is None or not check(raw) or len(raw) > 10 * 1024 * 1024:
                raise ValueError("Invalid image type or size")
            images += 1
            total += len(raw)
        else:
            raise ValueError("Only text and image blocks are accepted as user content")
    if images > 20 or total > 20 * 1024 * 1024:
        raise ValueError("Image input exceeds 20 images or 20 MiB")
    if not any(b.get("type") == "text" and b["text"].strip() for b in content) and not images:
        raise ValueError("Content must include text or an image")


class Turn(BaseModel):
    model: str
    session_id: str
    conversation: str | None = None
    system: str | None = Field(default=None, max_length=400000)
    content: list[dict] = Field(min_length=1, max_length=256)
    tools: list[dict] = Field(default_factory=list, max_length=64)
    effort: str | None = None
    json_schema: dict | None = None
    workspace: str

    @model_validator(mode="after")
    def validate_turn(self):
        if not MODEL.fullmatch(self.model) or not UUID.fullmatch(self.session_id) or (self.conversation and not UUID.fullmatch(self.conversation)):
            raise ValueError("Invalid model, session or conversation id")
        if self.effort is not None and self.effort not in EFFORT:
            raise ValueError("Invalid effort")
        validate_content(self.content)
        for tool in self.tools:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.get("name", "")) or not isinstance(tool.get("inputSchema"), dict):
                raise ValueError("Invalid client tool declaration")
            if set(tool) - {"name", "description", "inputSchema"}:
                raise ValueError("Unexpected client tool fields")
        if self.json_schema is not None and self.tools:
            raise ValueError("Structured output cannot be combined with client tools")
        return self


class ToolResult(BaseModel):
    run_id: str
    worker_call_id: str
    content: list[dict]
    is_error: bool = False


@app.post("/tool-result", dependencies=[Depends(authorize)])
async def tool_result(body: ToolResult):
    bridge = ACTIVE.get(body.run_id)
    if bridge is None:
        raise HTTPException(409, "Tool execution unavailable")
    for item in body.content:
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            continue
        if item.get("type") == "image" and item.get("mimeType") in IMAGE_SIGNATURES and isinstance(item.get("data"), str):
            continue
        raise HTTPException(400, "Only text and image tool results are supported")
    bridge.resolve(body.worker_call_id, {"content": body.content, "isError": body.is_error})
    return {"ok": True}


def usage_counts(usage):
    usage = usage or {}
    def count(name):
        value = usage.get(name, 0)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    uncached, read, write, out = (count(n) for n in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
    # Gateway billing and OpenAI usage treat cached input as a subset of total input.
    return {"input_tokens": uncached + read + write, "output_tokens": out,
            "cache_read_tokens": read, "cache_write_tokens": write}


@app.post("/turn", dependencies=[Depends(authorize)])
async def turn(body: Turn):
    workspace = Path(body.workspace).resolve()
    if workspace == ROOT or ROOT not in workspace.parents or not workspace.is_dir():
        raise HTTPException(400, "Invalid workspace")
    if login and login.task and not login.task.done():
        raise HTTPException(409, "Login in progress")
    if lock.locked() or len(active_turns) >= MAX_TURNS:
        raise HTTPException(409, "Worker capacity exhausted")
    ticket = body.conversation or body.session_id
    if ticket in active_turns:
        raise HTTPException(409, "Conversation already executing")
    active_turns[ticket] = body.conversation

    async def events():
        process = stderr_task = bridge = source = None
        temporary = tempfile.TemporaryDirectory(prefix="gateway-turn-", dir="/tmp")
        terminal = None
        produced_output = False
        thread = body.conversation or body.session_id
        hidden_blocks = set()
        try:
            bridge = ToolBridge(body.tools)
            ACTIVE[bridge.token] = bridge
            args = BASE_ARGS + ["--input-format", "stream-json", "--include-partial-messages", "--model", body.model]
            if body.effort:
                args += ["--effort", body.effort]
            if body.json_schema is not None:
                args += ["--json-schema", json.dumps(body.json_schema)]
            if body.system is not None:
                # Customer content stays out of process arguments.
                prompt_file = Path(temporary.name) / "system-prompt.txt"
                prompt_file.write_text(body.system)
                prompt_file.chmod(0o600)
                args += ["--system-prompt-file", str(prompt_file)]
            if body.tools:
                args += ["--mcp-config", bridge.mcp_config(), "--allowedTools", f"mcp__{SERVER_NAME}"]
            args += ["--resume", body.conversation] if body.conversation else ["--session-id", body.session_id]
            process = await asyncio.create_subprocess_exec(*args, cwd=str(workspace), env=cli_env(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True, limit=2**22)

            async def drain():
                tail = b""
                while chunk := await process.stderr.read(8192):
                    tail = (tail + chunk)[-8192:]
                return tail
            stderr_task = asyncio.create_task(drain())
            process.stdin.write((json.dumps({"type": "user", "message": {"role": "user", "content": body.content}}) + "\n").encode())
            await process.stdin.drain()
            process.stdin.close()
            source = bridge.messages(process.stdout)
            async with aclosing(source):
                async for item in source:
                    # Relay events carry a string "event"; CLI stream_event lines nest a dict there.
                    kind = item["event"] if isinstance(item.get("event"), str) else item.get("type")
                    if kind == "heartbeat":
                        yield json.dumps({"heartbeat": True}) + "\n"
                    elif kind == "client_tool":
                        produced_output = True
                        yield json.dumps({**item, "thread_id": thread}) + "\n"
                    elif kind == "system":
                        if item.get("subtype") == "init":
                            thread = item.get("session_id") or thread
                            if body.conversation and thread != body.conversation:
                                yield json.dumps({"error": "Claude resumed a different session", "kind": "session"}) + "\n"
                                return
                            if not body.conversation and thread != body.session_id:
                                yield json.dumps({"error": "Claude ignored the requested session id", "kind": "connection"}) + "\n"
                                return
                            active_turns[ticket] = thread
                            yield json.dumps({"event": "init", "thread_id": thread}) + "\n"
                    elif kind == "rate_limit_event":
                        remember_rate_limit(item.get("rate_limit_info"))
                        yield json.dumps({"event": "rate_limit", "thread_id": thread, "rate_limit_info": item.get("rate_limit_info")}) + "\n"
                    elif kind == "stream_event":
                        if body.json_schema is not None:
                            continue  # Deliver only the validated structured_output result.
                        event = item.get("event") or {}
                        etype = event.get("type")
                        index = event.get("index")
                        if etype == "content_block_start":
                            block = event.get("content_block") or {}
                            if block.get("type") == "tool_use":
                                # Relay calls surface through the MCP bridge; StructuredOutput is internal.
                                hidden_blocks.add(index)
                                continue
                        if index in hidden_blocks:
                            if etype == "content_block_stop":
                                hidden_blocks.discard(index)
                            continue
                        line = {"thread_id": thread, "stream": event}
                        if etype == "content_block_delta" and (event.get("delta") or {}).get("type") == "text_delta":
                            produced_output = True
                            line["delta"] = event["delta"].get("text", "")
                        yield json.dumps(line) + "\n"
                    elif kind == "result":
                        if item.get("subtype") != "success" or item.get("is_error"):
                            detail = str(item.get("result") or item.get("subtype") or "")
                            failure = error_kind(detail + " " + str(item.get("api_error_status") or ""))
                            yield json.dumps({"error": "Claude execution failed: " + (item.get("subtype") or failure), "kind": failure}) + "\n"
                            return
                        structured = item.get("structured_output")
                        if body.json_schema is not None:
                            if structured is None:
                                yield json.dumps({"error": "Claude returned no structured output", "kind": "request"}) + "\n"
                                return
                            text = json.dumps(structured, ensure_ascii=False)
                            produced_output = True
                            yield json.dumps({"thread_id": thread, "delta": text,
                                              "stream": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}}) + "\n"
                        if not produced_output:
                            yield json.dumps({"error": "Claude reported success without any response or client tool call", "kind": "connection"}) + "\n"
                            return
                        terminal = {"thread_id": thread, "done": True, "stop_reason": item.get("stop_reason") or "end_turn",
                                    "num_turns": item.get("num_turns"), "usage": item.get("usage"),
                                    "model_usage": item.get("modelUsage"), **usage_counts(item.get("usage"))}
                        break
            if terminal is None:
                await process.wait()
                tail = await stderr_task
                yield json.dumps({"error": "Claude ended without a final result", "kind": error_kind(tail.decode(errors="replace"))}) + "\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield json.dumps({"error": "Claude execution interrupted or timed out: " + type(exc).__name__, "kind": "connection"}) + "\n"
        finally:
            # StreamingResponse cancellation propagates here on client disconnect.
            try:
                with anyio.CancelScope(shield=True):
                    await stop(process)
                    if stderr_task:
                        await asyncio.gather(stderr_task, return_exceptions=True)
            finally:
                try:
                    if bridge:
                        bridge.close()
                finally:
                    temporary.cleanup()
                    active_turns.pop(ticket, None)
        # The terminal line promises the session can be resumed immediately.
        if terminal is not None:
            yield json.dumps(terminal) + "\n"
    return StreamingResponse(events(), media_type="application/x-ndjson")


# --- Subscription login through the official CLI, driven over a pty ---------------

URL_PATTERN = re.compile(r"https://claude\.com/[^\s\x00-\x1f\x7f]+")
CSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]")


class Login:
    def __init__(self):
        self.id = uuid4().hex
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.expires = time.monotonic() + 600
        self.fd = self.process = self.task = None
        self.text = ""
        self.url = None
        self.account = None
        self.error = None
        self.code_submitted = False
        self.redacted = []
        self.owns_lock = False
        self.input_lock = asyncio.Lock()

    async def start(self):
        master, slave = pty.openpty()
        self.fd = master
        os.set_blocking(master, False)
        self.process = await asyncio.create_subprocess_exec("claude", "auth", "login", "--claudeai",
            stdin=slave, stdout=slave, stderr=slave, cwd=str(ROOT),
            env={**cli_env(), "TERM": "xterm-256color", "BROWSER": "/bin/false"}, start_new_session=True)
        os.close(slave)
        self.task = asyncio.create_task(self.read())

    def plain(self):
        text = CSI.sub("", self.text)
        for secret in self.redacted:
            text = text.replace(secret, "[submitted]")
        return text

    async def read(self):
        try:
            # A child can exit before the next PTY read; drain its buffered output.
            while time.monotonic() < self.expires:
                try:
                    data = os.read(self.fd, 65536)
                except BlockingIOError:
                    if self.process.returncode is not None:
                        break
                    await asyncio.sleep(.1)
                    continue
                except OSError:
                    break
                if not data:
                    break
                self.text = (self.text + self.decoder.decode(data))[-65536:]
                plain = CSI.sub("", self.text)
                if not self.url:
                    match = URL_PATTERN.search(plain)
                    if match:
                        self.url = match.group(0)
                if re.search(r"Login successful|Logged in as|Successfully logged in", plain):
                    account, kind = await read_account()
                    if account:
                        self.account = account
                    else:
                        self.error = "登录已完成，但官方 CLI 未返回账号信息，请重试"
                    break
                if self.code_submitted and re.search(r"(?i)invalid|expired|failed|denied|error", plain[-800:]):
                    self.error = "Claude 授权失败。授权码可能无效、已过期或已被使用；请关闭窗口后重新登录并获取新的授权码。"
                    break
            if not self.account and not self.error and self.process.returncode is None:
                # PTY EOF can precede asyncio's child-exit notification.
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
            if not self.account and not self.error and self.code_submitted and self.process.returncode == 0:
                # CLI success is authoritative even when its terminal wording changes.
                # Do not accept old credentials after cancellation or a failed exit.
                account, _ = await read_account()
                if account:
                    self.account = account
            if not self.account and not self.error:
                self.error = ("登录会话已超时，请重新开始" if time.monotonic() >= self.expires
                              else "Claude 登录进程已结束，但未确认登录成功，请重新开始")
        except Exception:
            self.error = "无法确认 Claude 登录结果，请稍后探测账号或重试"
        finally:
            await stop(self.process)
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            if self.owns_lock:
                self.owns_lock = False
                lock.release()

    def state(self):
        plain = self.plain()
        if self.account:
            view = {"stage": "done", "title": "登录成功"}
        elif self.url and ("Paste code" in plain or "code" in plain.lower()) and not self.code_submitted:
            view = {"stage": "authorize", "title": "完成 Claude 授权",
                    "message": "打开 Claude 授权页面，使用拥有订阅权益的账号完成登录，然后把页面显示的授权码粘贴到下方。",
                    "login_url": self.url}
        elif self.code_submitted:
            view = {"stage": "waiting", "title": "正在完成 Claude 授权", "message": "授权码已提交，正在等待登录服务响应，请勿重复提交。"}
        else:
            view = {"stage": "waiting", "title": "正在准备登录", "message": "正在等待官方 CLI 生成授权链接，请稍候。"}
        return {"session_id": self.id, **view, "logged_in": bool(self.account) and bool(self.task and self.task.done()),
                "account": self.account, "error": self.error, "expires_in": max(0, int(self.expires - time.monotonic()))}


class LoginInput(BaseModel):
    session_id: str
    action: str = "enter"
    code: str = Field(default="", max_length=4096)
    menu_id: str = ""


async def wait_for_login_view(session, timeout=8):
    deadline = time.monotonic() + timeout
    state = session.state()
    while state.get("stage") == "waiting" and not state.get("error") and session.task and not session.task.done() and time.monotonic() < deadline:
        await asyncio.sleep(.1)
        state = session.state()
    return state


@app.post("/login/start", dependencies=[Depends(authorize)])
async def login_start():
    global login
    if login and login.task and not login.task.done():
        return await wait_for_login_view(login)
    if busy():
        raise HTTPException(409, "Worker is executing")
    await lock.acquire()
    login = Login()
    login.owns_lock = True
    try:
        await login.start()
    except BaseException:
        login.owns_lock = False
        lock.release()
        raise
    return await wait_for_login_view(login)


@app.post("/login/status", dependencies=[Depends(authorize)])
async def login_status(body: LoginInput):
    if not login or login.id != body.session_id:
        raise HTTPException(404, "Login session expired")
    return login.state()


@app.post("/login/verify", dependencies=[Depends(authorize)])
async def login_verify(body: LoginInput):
    session = login
    if not session or session.id != body.session_id:
        raise HTTPException(404, "Login session expired")
    if not session.account or not session.task or not session.task.done():
        raise HTTPException(409, "Login is not complete")
    return await probe()


async def close_login(session):
    if session and session.task and not session.task.done():
        await stop(session.process)
        await session.task


@app.post("/login/input", dependencies=[Depends(authorize)])
async def login_input(body: LoginInput):
    if not login or login.id != body.session_id or login.fd is None:
        raise HTTPException(409, "Login is not active")
    if body.action == "cancel":
        await close_login(login)
        return {"message": "登录已取消"}
    if body.action != "code":
        raise HTTPException(400, "Invalid login action")
    if not body.code or any(ord(c) < 32 or ord(c) == 127 for c in body.code):
        raise HTTPException(400, "Invalid authorization code")
    if login.state()["stage"] != "authorize":
        raise HTTPException(409, "CLI is not waiting for an authorization code")
    async with login.input_lock:
        if login.code_submitted:
            raise HTTPException(409, "授权码已提交，请等待登录结果或重新开始")
        login.code_submitted = True
        login.redacted.append(body.code)
        os.write(login.fd, body.code.encode())
        await asyncio.sleep(.15)
        if login.fd is None:
            raise HTTPException(409, "Login is no longer active")
        os.write(login.fd, b"\r")
    return {"message": "已提交"}


@app.post("/login/logout", dependencies=[Depends(authorize)])
async def logout():
    global login
    await close_login(login)
    if busy():
        raise HTTPException(409, "Worker is busy; retry after the current operation")
    async with lock:
        await run_json(["claude", "auth", "logout"], timeout=30)
        account, _ = await read_account()
        if account and account.get("authMethod") != "none":
            raise HTTPException(502, "Official CLI did not sign out; credentials may be environment-provided")
        login = None
        import shutil
        shutil.rmtree(SESSION_ROOT, ignore_errors=True)
        rate_limit.update(info=None, at=0.0)
    return {"logged_in": False, "account": None, "message": "已退出 Claude 登录"}


class PruneInput(BaseModel):
    keep_ids: list[str] = Field(default_factory=list)
    min_age_seconds: int = Field(default=86400, ge=3600)


@app.post("/sessions/prune", dependencies=[Depends(authorize)])
async def prune_sessions(body: PruneInput):
    # Never race login/account changes or delete in-flight sessions.
    if lock.locked() or (login and login.task and not login.task.done()):
        raise HTTPException(409, "Worker is busy")
    import shutil
    keep = set(body.keep_ids) | set(active_turns) | {v for v in active_turns.values() if v}
    cutoff, removed = time.time() - body.min_age_seconds, 0
    async with lock:
        for project in SESSION_ROOT.iterdir() if SESSION_ROOT.exists() else ():
            if not project.is_dir() or project.is_symlink():
                continue
            for path in project.glob("*.jsonl"):
                if not UUID.fullmatch(path.stem) or path.stem in keep or path.is_symlink():
                    continue
                if path.stat().st_mtime >= cutoff:
                    continue
                path.unlink(missing_ok=True)
                subagents = project / path.stem
                if subagents.is_dir() and not subagents.is_symlink():
                    shutil.rmtree(subagents)
                removed += 1
    return {"removed": removed}
