"""Private Antigravity worker HTTP transport. Never publish its port externally."""
import asyncio
import anyio
import codecs
import fcntl
import hmac
import hashlib
import json
import os
import pty
import re
import signal
import struct
import termios
import time
from pathlib import Path
from contextlib import aclosing
from uuid import uuid4
import pyte
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from client_bridge import ToolBridge, ACTIVE, install_mcp, execution_environment

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
install_mcp(app)
ROOT = Path("/workspace")
ACCOUNT = Path.home() / ".gemini/antigravity-cli/gateway-account.json"
lock = asyncio.Lock()
login = None
active_turns = {}
MAX_TURNS = 4

def busy():
    return lock.locked() or bool(active_turns)

MODEL = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,119}$")
UUID = re.compile(r"^[a-f0-9-]{36}$")

def authorize(authorization: str | None = Header(default=None)):
    token = os.environ.get("CODEX_WORKER_TOKEN", "")
    if not token or not hmac.compare_digest(authorization or "", "Bearer " + token):
        raise HTTPException(401, "unauthorized")

def error_kind(text):
    text = text.lower()
    if "eligibility check failed" in text or "not eligible for antigravity" in text:
        return "ineligible"
    if any(x in text for x in ("quota", "429", "resource_exhausted", "rate limit", "usage limit")):
        return "limit"
    if any(x in text for x in ("unauthenticated", "not logged", "unauthorized", "sign in", "login required")):
        return "logged_out"
    return "connection"

async def stop(process):
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGINT)
        await asyncio.wait_for(process.wait(), 5)
    except asyncio.TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
    except ProcessLookupError:
        await process.wait()

def account_file():
    try:
        return json.loads(ACCOUNT.read_text())
    except (OSError, ValueError):
        return None


def signed_in_header(display):
    """Recognize the CLI account header without requiring a subscription label."""
    lines = display.splitlines()
    for index, line in enumerate(lines):
        if re.search(r"Antigravity CLI \d+\.", line):
            return bool(re.search(r"[\w.+-]+@[\w.-]+", "\n".join(lines[index:index + 4])))
    return bool(re.search(r"[\w.+-]+@[\w.-]+\s+\([^)]+\)", display))


def signed_out_screen(display):
    text = display.lower()
    if "signing in" in text:
        return False
    return any(marker in text for marker in (
        "select login method:",
        "successfully logged out", "signed out from server:", "you have been logged out"))


def authorization_code_error(display):
    """Map CLI OAuth failures to a safe message without exposing terminal output."""
    text = re.sub(r"\s+", " ", display).lower()
    explicit = (
        "invalid_grant", "authorization code is invalid", "invalid authorization code",
        "authorization code has expired", "authorization code was already",
        "failed to exchange authorization code", "error exchanging authorization code",
    )
    failed = any(term in text for term in ("invalid", "expired", "already used", "already redeemed",
                                            "failed", "failure", "error", "denied", "unable"))
    if any(term in text for term in explicit) or failed:
        return "Google 授权失败。授权码可能无效、已过期或已被使用；请关闭此窗口，重新点击登录账号并获取新的授权码。"
    return None


def login_view(display, url=None):
    """Expose structured login controls, never terminal output or entered secrets."""
    if "Do you trust the contents of this project?" in display:
        selected = 0 if re.search(r">\s*Yes, I trust this folder", display) else (
            1 if re.search(r">\s*No, exit", display) else None)
        if selected is not None:
            return {"stage": "choose", "title": "确认 Worker 工作区权限",
                    "message": "Antigravity 请求读取、编辑和执行 Worker 工作区中的文件。"
                               "选择信任后继续登录；不信任则退出。",
                    "options": [{"id": 0, "label": "信任 Worker 工作区并继续"},
                                {"id": 1, "label": "不信任，退出"}],
                    "selected": selected, "menu_id": "workspace-trust"}
    if "Terms of Service & Data Use" in display and re.search(r"\[[xX ]\] Yes, I agree", display):
        checked = bool(re.search(r"\[[xX]\] Yes, I agree", display))
        return {"stage": "choose", "title": "服务条款与交互数据使用",
                "message": "CLI 请求确认服务条款，并选择是否允许 Google 收集和使用交互数据。"
                           "请先阅读条款和隐私政策，再选择继续方式。相关链接："
                           + " ".join(re.findall(r"https://[^\s]+", display)),
                "options": [{"id": 0, "label": "不允许收集交互数据，确认条款并继续"},
                            {"id": 1, "label": "允许收集交互数据，确认条款并继续"}],
                "selected": int(checked), "menu_id": "onboarding-terms-data-" + str(int(checked))}
    if "Terms of Service & Data Use" in display and re.search(r">\s*Done", display):
        return {"stage": "choose", "title": "服务条款与数据使用",
                "message": "请阅读 Antigravity CLI 显示的服务条款与数据使用说明后确认。"
                           "CLI 提示：AI 编程代理可执行代码，需检查其操作；"
                           "CLI 不收集提示词、内容或模型回复，但会收集功能使用等产品分析数据。"
                           "相关链接：" + " ".join(re.findall(r"https://[^\s]+", display)),
                "options": [{"id": 0, "label": "确认条款与数据说明并继续"}],
                "selected": 0, "menu_id": "onboarding-terms"}
    options = []
    selected = None
    for line in display.splitlines():
        match = re.match(r"^\s*([>❯›]?)\s*(\d+)\.\s+(.+?)\s*$", line)
        if match:
            index = len(options)
            label = match[3].strip()
            translations = {"Continue with Google Cloud": "使用 Google Cloud 企业账号登录",
                            "Other sign-in options": "其他登录方式",
                            "Continue with Google": "使用 Google 账号登录",
                            "Google OAuth": "使用 Google OAuth 登录",
                            "Use a Google Cloud project": "使用 Google Cloud 企业账号登录"}
            options.append({"id": index, "label": translations.get(label, label)})
            if match[1]:
                selected = index
    lower = display.lower()
    if options and selected is not None:
        title = "选择登录方式"
        if "project" in lower: title = "选择 Google Cloud 项目"
        elif "license" in lower or "subscription" in lower: title = "选择订阅许可证"
        elif "account" in lower and "login method" not in lower: title = "选择账号"
        menu_id = hashlib.sha256(json.dumps(options).encode()).hexdigest()[:16]
        return {"stage": "choose", "title": title, "options": options,
                "selected": selected, "menu_id": menu_id}
    if url and "paste" in lower and "code" in lower:
        return {"stage": "authorize", "title": "完成 Google 授权",
                "message": "打开 Google 授权页面，完成企业 SSO 登录后，将页面显示的授权码粘贴到下方。",
                "login_url": url}
    return {"stage": "waiting", "title": "正在准备登录",
            "message": "正在等待登录服务响应，请稍候。"}

async def wait_for_login_view(session, timeout=8):
    """Give the CLI time to draw its first actionable screen."""
    deadline = time.monotonic() + timeout
    state = session.state()
    while (state.get("stage") == "waiting" and not state.get("error")
           and session.task and not session.task.done() and time.monotonic() < deadline):
        await asyncio.sleep(.1)
        state = session.state()
    return state

async def wait_login_screen(session, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while session.fd is not None and time.monotonic() < deadline:
        display = "\n".join(session.screen.display)
        if predicate(display):
            return display
        await asyncio.sleep(.05)
    raise HTTPException(409, "登录页面尚未就绪，请重试")


async def focus_terms(session, focus):
    patterns = {"checkbox": r">\s*\[[xX ]\]",
                "previous": r">\s*Previous", "done": r">\s*Done"}
    for _ in range(3):
        display = "\n".join(session.screen.display)
        if re.search(patterns[focus], display):
            return display
        current = next((key for key, pattern in patterns.items() if re.search(pattern, display)), None)
        if current is None:
            raise HTTPException(409, "无法定位条款控件，请重试")
        os.write(session.fd, b"\t")
        await wait_login_screen(session, lambda text: "Terms of Service & Data Use" in text
                                and not re.search(patterns[current], text)
                                and any(re.search(pattern, text) for pattern in patterns.values()))
    raise HTTPException(409, "无法定位条款确认按钮，请重试")


class Login:
    def __init__(self):
        self.id = uuid4().hex
        self.screen = pyte.Screen(140, 45)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.expires = time.monotonic() + 600
        self.fd = None
        self.process = None
        self.url = None
        self.account = None
        self.unavailable_kind = None
        self.verification_task = None
        self.error = None
        self.task = None
        self.redacted = []
        self.code_submitted = False
        self.owns_lock = False
        self.input_lock = asyncio.Lock()

    async def start(self):
        master, slave = pty.openpty()
        self.fd = master
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 140, 0, 0))
        os.set_blocking(master, False)
        self.process = await asyncio.create_subprocess_exec("agy", stdin=slave, stdout=slave, stderr=slave,
            cwd=str(ROOT), env={**os.environ, "TERM": "xterm-256color", "SSH_CONNECTION": "web 0 worker 0"},
            start_new_session=True)
        os.close(slave)
        self.task = asyncio.create_task(self.read())

    async def read(self):
        raw = ""
        theme_confirmed = False
        try:
            while self.process.returncode is None and time.monotonic() < self.expires:
                try:
                    data = os.read(self.fd, 65536)
                except BlockingIOError:
                    await asyncio.sleep(.1)
                    continue
                except OSError:
                    break
                if not data:
                    break
                text = self.decoder.decode(data)
                raw = (raw + text)[-65536:]
                # URL comes from the official CLI's OSC hyperlink or plain text.
                match = re.search(r"https://accounts\.google\.com/o/oauth2/auth\?[^\s\x00-\x1f\x7f]+", raw)
                if match:
                    self.url = match.group(0)
                self.stream.feed(text)
                display = "\n".join(self.screen.display)
                if not theme_confirmed and "Choose your color scheme:" in display and "enter Confirm" in display:
                    theme_confirmed = True
                    os.write(self.fd, b"\r")
                    continue
                if error_kind(display) == "ineligible":
                    self.unavailable_kind = "ineligible"
                    header = display.split("Eligibility Check", 1)[0]
                    identity = re.search(r"([\w.+-]+@[\w.-]+)", header)
                    if identity and "Antigravity CLI" in header:
                        self.account = {"type": "google-subscription", "email": identity[1],
                                        "planType": None, "project": None}
                        ACCOUNT.write_text(json.dumps(self.account))
                        ACCOUNT.chmod(0o600)
                    self.error = "账号已认证，但未通过 Antigravity 资格检查，请更换账号或联系管理员。"
                    break
                email = re.search(r"([\w.+-]+@[\w.-]+)\s+\(([^)]+)\)", display)
                project = re.search(r"GCP Project:\s*([\w.-]+)", display)
                if email and project:
                    self.account = {"type": "google-subscription", "email": email[1],
                                    "planType": email[2], "project": project[1]}
                    ACCOUNT.write_text(json.dumps(self.account))
                    ACCOUNT.chmod(0o600)
                    break
            if not self.account and not self.error:
                self.error = "登录已结束或超时，请重新开始"
        finally:
            await stop(self.process)
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            if self.owns_lock:
                self.owns_lock = False
                lock.release()

    def state(self):
        display = "\n".join(line.rstrip() for line in self.screen.display).strip()
        for secret in self.redacted:
            display = display.replace(secret, "[已提交]")
        prompt_error = authorization_code_error(display) if self.code_submitted and not self.unavailable_kind else None
        view = login_view(display, self.url)
        if self.code_submitted and view["stage"] == "authorize" and not prompt_error:
            view = {"stage": "waiting", "title": "正在完成 Google 授权",
                    "message": "授权码已提交，正在等待登录服务响应，请勿重复提交。"}
        return {"session_id": self.id, **view,
                "logged_in": bool(self.account) and bool(self.task and self.task.done()), "account": self.account,
                "error": prompt_error or self.error, "expires_in": max(0, int(self.expires - time.monotonic()))}

class LoginInput(BaseModel):
    session_id: str
    action: str = "enter"
    code: str = Field(default="", max_length=4096)
    menu_id: str = ""

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
    if session.verification_task is None:
        session.verification_task = asyncio.create_task(probe())
    return await asyncio.shield(session.verification_task)


@app.post("/login/input", dependencies=[Depends(authorize)])
async def login_input(body: LoginInput):
    if not login or login.id != body.session_id or login.fd is None:
        raise HTTPException(409, "Login is not active")
    if body.action == "cancel":
        await close_login(login)
        return {"message": "登录已取消"}
    if body.action.startswith("select:"):
        async with login.input_lock:
            view = login.state()
            try:
                choice = int(body.action.split(":", 1)[1])
            except ValueError:
                raise HTTPException(400, "Invalid login choice")
            if view.get("stage") != "choose" or body.menu_id != view.get("menu_id") or not 0 <= choice < len(view["options"]):
                raise HTTPException(409, "登录选项已更新，请重新选择")
            if view["menu_id"].startswith("onboarding-terms-data-"):
                # Tab cycles checkbox -> Previous -> Done; wait for each redraw.
                display = await focus_terms(login, "checkbox")
                checked = bool(re.search(r"\[[xX]\] Yes, I agree", display))
                if checked != bool(choice):
                    os.write(login.fd, b"\r")
                    await wait_login_screen(login, lambda text:
                        bool(re.search(r"\[[xX]\] Yes, I agree", text)) == bool(choice)
                        and "Terms of Service & Data Use" in text)
                await focus_terms(login, "done")
                os.write(login.fd, b"\r")
                return {"message": "已确认"}
            delta = choice - view["selected"]
            if delta:
                os.write(login.fd, (b"\x1b[B" if delta > 0 else b"\x1b[A") * abs(delta))
                await asyncio.sleep(.15)
            os.write(login.fd, b"\r")
        return {"message": "已选择"}
    keys = {"up": b"\x1b[A", "down": b"\x1b[B", "enter": b"\r", "escape": b"\x1b"}
    if body.action == "code":
        if not body.code or any(ord(c) < 32 or ord(c) == 127 for c in body.code):
            raise HTTPException(400, "Invalid authorization code")
        # Accept code only on the CLI's authorization-code prompt, never a shell/chat input.
        display = "\n".join(login.screen.display).lower()
        if login_view(display, login.url)["stage"] != "authorize":
            raise HTTPException(409, "CLI is not waiting for an authorization code")
        async with login.input_lock:
            if login.code_submitted:
                raise HTTPException(409, "授权码已提交，请等待登录结果或重新开始")
            login.code_submitted = True
            login.redacted.append(body.code)
            # Bubble Tea distinguishes pasted text from a separate Enter key.
            # Sending both in one read can insert the code without submitting it.
            os.write(login.fd, body.code.encode())
            await asyncio.sleep(.15)
            if login.fd is None:
                raise HTTPException(409, "Login is no longer active")
            os.write(login.fd, b"\r")
        return {"message": "已提交"}
    elif body.action in keys:
        data = keys[body.action]
    else:
        raise HTTPException(400, "Invalid login action")
    os.write(login.fd, data)
    return {"message": "已提交"}

@app.post("/account", dependencies=[Depends(authorize)])
async def account():
    if login and login.task and not login.task.done():
        raise HTTPException(409, "Login in progress")
    if busy():
        cached = account_file()
        if cached:
            return {"account": cached}
        raise HTTPException(409, "Worker busy")
    async with lock:
        proc = await asyncio.create_subprocess_exec("agy", "models", stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 30)
            if proc.returncode != 0:
                return {"account": None, "kind": error_kind(stderr.decode(errors="replace"))}
        finally:
            await stop(proc)
        # Read the official CLI header, so external account/project changes are
        # detected instead of trusting metadata cached at the first login.
        identity = Login()
        identity.expires = time.monotonic() + 8
        await identity.start()
        await identity.task
    return {"account": identity.account, "kind": identity.unavailable_kind, "available": not bool(identity.unavailable_kind), "models": [line.split()[0] for line in stdout.decode().splitlines() if line.startswith("gemini-")]}



async def cli_panel(command):
    """Only fixed official slash commands; never accept arbitrary user input."""
    if command not in {"/usage", "/logout"}:
        raise ValueError("Unsupported CLI command")
    if busy():
        raise HTTPException(409, "Worker is busy; retry after the current operation")
    async with lock:
        master, slave = pty.openpty()
        process = None
        screen = pyte.Screen(160, 60)
        stream = pyte.Stream(screen)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 160, 0, 0))
            os.set_blocking(master, False)
            process = await asyncio.create_subprocess_exec("agy", stdin=slave, stdout=slave, stderr=slave,
                cwd=str(ROOT), env={**os.environ, "TERM": "xterm-256color", "SSH_CONNECTION": "web 0 worker 0"},
                start_new_session=True)
            os.close(slave)
            slave = None
            deadline = time.monotonic() + 25
            sent = None
            confirmed = False
            display = ""
            while time.monotonic() < deadline:
                try:
                    data = os.read(master, 65536)
                except BlockingIOError:
                    await asyncio.sleep(.1)
                    continue
                except OSError:
                    break
                if not data:
                    break
                stream.feed(decoder.decode(data))
                display = "\n".join(line.rstrip() for line in screen.display)
                if command == "/logout" and signed_out_screen(display):
                    return display
                if sent is None and signed_in_header(display):
                    os.write(master, command.encode())
                    await asyncio.sleep(.15)
                    os.write(master, b"\r")
                    sent = time.monotonic()
                if sent is not None:
                    if command == "/logout" and not confirmed and "Are you sure you want to sign out?" in display:
                        os.write(master, b"y")
                        await asyncio.sleep(.15)
                        os.write(master, b"\r")
                        confirmed = True
                    if command == "/usage" and "Models & Quota" in display and (
                        "consumption options" in display or "%" in display or "failed" in display.lower()):
                        return display
                if process.returncode is not None:
                    break
            raise HTTPException(502, "Official CLI did not confirm the operation; retry later")
        finally:
            with anyio.CancelScope(shield=True):
                if process is not None:
                    await stop(process)
                os.close(master)
                if slave is not None:
                    os.close(slave)


def usage_panel(display):
    if "Models & Quota" not in display:
        raise HTTPException(502, "No quota panel returned")
    if "For Antigravity Business consumption options" in display:
        return {"buckets": [], "available": False, "message": "该企业套餐未通过官方 CLI 返回数值额度，请在企业控制台查看。",
                "help_url": "https://antigravity.google/docs/enterprise"}
    # Preserve provider labels/windows rather than inventing OpenAI 5-hour/week windows.
    lines = [line.strip() for line in display.splitlines() if "%" in line and "@" not in line]
    if lines:
        return {"buckets": [], "available": True, "message": "\n".join(lines)}
    raise HTTPException(502, "Quota values were not returned by the official CLI")


@app.post("/rate-limits", dependencies=[Depends(authorize)])
async def rate_limits():
    return usage_panel(await cli_panel("/usage"))


async def close_login(session):
    """Wait for the reader to release its lock before allowing another login."""
    if session and session.task and not session.task.done():
        await stop(session.process)
        await session.task


@app.post("/login/logout", dependencies=[Depends(authorize)])
async def logout():
    global login
    await close_login(login)
    await cli_panel("/logout")
    ACCOUNT.unlink(missing_ok=True)
    login = None
    return {"logged_in": False, "account": None, "message": "已退出 Gemini 登录"}

class ImageAttachment(BaseModel):
    mimeType: str
    data: str = Field(min_length=1, max_length=13981016)

    @model_validator(mode="after")
    def validate_image(self):
        import base64
        try:
            raw = base64.b64decode(self.data, validate=True)
        except ValueError:
            raise ValueError("Invalid image base64") from None
        signatures = {"image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
                      "image/jpeg": raw.startswith(b"\xff\xd8\xff"),
                      "image/gif": raw.startswith((b"GIF87a", b"GIF89a")),
                      "image/webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"}
        if len(raw) > 10 * 1024 * 1024 or not signatures.get(self.mimeType):
            raise ValueError("Invalid image type or size")
        return self


class Turn(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000000)
    model: str
    conversation: str | None = None
    workspace: str
    tools: list[dict] = Field(default_factory=list, max_length=64)
    images: list[ImageAttachment] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_images(self):
        if sum(len(i.data) // 4 * 3 - len(i.data) + len(i.data.rstrip("=")) for i in self.images) > 20 * 1024 * 1024:
            raise ValueError("Image input exceeds 20 MiB")
        return self


class ToolResult(BaseModel):
    run_id: str
    worker_call_id: str
    content: list[dict]
    is_error: bool = False


@app.post("/capabilities", dependencies=[Depends(authorize)])
async def capabilities():
    return {"client_tools": 1, "image_input": 1, "tool_result_types": ["text"]}


@app.post("/tool-result", dependencies=[Depends(authorize)])
async def tool_result(body: ToolResult):
    bridge = ACTIVE.get(body.run_id)
    if bridge is None:
        raise HTTPException(409, "Tool execution unavailable")
    if any(item.get("type") != "text" or not isinstance(item.get("text"), str) for item in body.content):
        raise HTTPException(400, "Only text tool results are supported")
    bridge.resolve(body.worker_call_id, {"content": body.content, "isError": body.is_error})
    return {"ok": True}

@app.post("/turn", dependencies=[Depends(authorize)])
async def turn(body: Turn):
    if not MODEL.fullmatch(body.model) or (body.conversation and not UUID.fullmatch(body.conversation)):
        raise HTTPException(400, "Invalid model or conversation")
    workspace = Path(body.workspace).resolve()
    if workspace == ROOT or ROOT not in workspace.parents or not workspace.is_dir():
        raise HTTPException(400, "Invalid workspace")
    if login and login.task and not login.task.done():
        raise HTTPException(409, "Login in progress")
    if lock.locked() or len(active_turns) >= MAX_TURNS:
        raise HTTPException(409, "Worker capacity exhausted")
    ticket = body.conversation or uuid4().hex
    if body.conversation and body.conversation in active_turns.values():
        raise HTTPException(409, "Conversation already executing")
    active_turns[ticket] = body.conversation

    async def events():
        process = None
        stderr_task = None
        bridge = None
        configuration = None
        configured = False
        source = None
        environment = None
        terminal = None
        produced_output = False
        try:
            if any(not re.fullmatch(r"gateway_client_\d+", tool.get("name", "")) or
                   not isinstance(tool.get("inputSchema"), dict) for tool in body.tools):
                raise ValueError("Invalid client tool declaration")
            bridge = ToolBridge(body.tools, images=[image.model_dump() for image in body.images])
            environment = execution_environment(workspace)
            isolated_home, isolated_workspace = environment.__enter__()
            configuration = bridge.configuration(isolated_workspace, home=isolated_home)
            configuration.__enter__()
            configured = True
            # The prompt is sent through stdin; never place customer content in process arguments.
            args = ["agy", "--input-format", "stream-json", "--output-format", "stream-json",
                    "--model", body.model, "--disable-slash-commands"]
            if body.conversation:
                args += ["--conversation", body.conversation]
            process = await asyncio.create_subprocess_exec(*args, cwd=str(isolated_workspace),
                env={**os.environ, "HOME": str(isolated_home)},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=2**21)
            async def drain():
                tail = b""
                while chunk := await process.stderr.read(8192):
                    tail = (tail + chunk)[-8192:]
                return tail
            stderr_task = asyncio.create_task(drain())
            prompt = body.prompt
            if body.images:
                prompt = ("Before answering, inspect ALL " + str(len(body.images)) +
                          " attached images using gateway_read_image with indices 1 through " + str(len(body.images)) +
                          ". This internal attachment reader is allowed alongside remote client tools.\n\n" + prompt)
            process.stdin.write((json.dumps({"event": "user", "message": {"content": prompt}}) + "\n").encode())
            await process.stdin.drain()
            thread = body.conversation or ""
            counts = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
            source = bridge.messages(process.stdout)
            async with aclosing(source):
                async for item in source:
                    if item.get("event") == "heartbeat":
                        yield json.dumps({"heartbeat": True}) + "\n"
                        continue
                    if item.get("event") == "client_tool":
                        produced_output = True
                        if not thread:
                            raise ValueError("Tool call before conversation initialization")
                        yield json.dumps({**item, "thread_id": thread, **counts,
                                          "input_tokens": counts["input_tokens"] + counts["cache_read_tokens"]}) + "\n"
                        continue
                    if item.get("event") == "init":
                        thread = item.get("conversation_id") or thread
                        active_turns[ticket] = thread
                        if body.conversation and thread != body.conversation:
                            yield json.dumps({"error": "Gemini resumed a different conversation", "kind": "session"}) + "\n"
                            return
                    elif item.get("event") == "step_update":
                        step = item.get("step_update", {})
                        # Step usage belongs to this invocation; final result usage is cumulative.
                        if step.get("state") == "DONE":
                            for key in counts:
                                counts[key] += int((step.get("usage") or {}).get(key, 0) or 0)
                        if step.get("step_type") == "agent_response" and step.get("text_delta"):
                            produced_output = True
                            if len(bridge.images_read) != len(body.images):
                                yield json.dumps({"error": "Gemini did not inspect every attached image", "kind": "request"}) + "\n"
                                return
                            yield json.dumps({"thread_id": thread, "delta": step["text_delta"]}) + "\n"
                    elif item.get("event") == "result":
                        result = item.get("result", {})
                        if result.get("status") != "SUCCESS":
                            kind = error_kind(json.dumps(result))
                            yield json.dumps({"error": "Gemini execution failed: " + kind, "kind": kind}) + "\n"
                            return
                        if not thread:
                            raise ValueError("Missing conversation id")
                        if len(bridge.images_read) != len(body.images):
                            yield json.dumps({"error": "Gemini did not inspect every attached image", "kind": "request"}) + "\n"
                            return
                        if not produced_output:
                            yield json.dumps({"error": "Gemini CLI reported success without delivering any response or client tool call; resume with a new user turn", "kind": "connection"}) + "\n"
                            return
                        # Antigravity reports uncached input separately; OpenAI usage
                        # and gateway billing require cached input as a subset of total input.
                        counts["input_tokens"] += counts["cache_read_tokens"]
                        terminal = {"thread_id": thread, "done": True, **counts}
                        break
            if terminal is None:
                await process.wait()
                tail = await stderr_task
                kind = error_kind(tail.decode(errors="replace"))
                yield json.dumps({"error": "Gemini ended without a final result", "kind": kind}) + "\n"
        except asyncio.CancelledError:
            raise
        except Exception:
            yield json.dumps({"error": "Gemini execution interrupted or timed out", "kind": "connection"}) + "\n"
        finally:
            # StreamingResponse cancellation propagates here on client disconnect.
            try:
                with anyio.CancelScope(shield=True):
                    if process:
                        await stop(process)
                    if stderr_task:
                        await asyncio.gather(stderr_task, return_exceptions=True)
            finally:
                try:
                    if bridge:
                        bridge.close()
                    if configured:
                        configuration.__exit__(None, None, None)
                finally:
                    try:
                        if environment:
                            environment.__exit__(None, None, None)
                    finally:
                        active_turns.pop(ticket, None)
        # A terminal response promises the conversation can be resumed immediately.
        # Finish process/configuration cleanup before exposing that boundary.
        if terminal is not None:
            yield json.dumps(terminal) + "\n"
    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.post("/probe", dependencies=[Depends(authorize)])
async def probe():
    state = await account()
    if not state.get("account") or state.get("available") is False:
        return state
    if busy():
        raise HTTPException(409, "Worker busy")
    async with lock:
        models = state.get("models") or []
        if not models:
            return {"account": None, "kind": "request"}
        proc = await asyncio.create_subprocess_exec("agy", "--model", models[0], "--print",
            "Reply OK only. Do not use tools.", "--output-format", "json", "--print-timeout", "30s",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 40)
            if proc.returncode or '"SUCCESS"' not in stdout.decode(errors="replace"):
                return {**state, "available": False, "kind": error_kind((stdout + stderr).decode(errors="replace"))}
        finally:
            await stop(proc)
    return state
