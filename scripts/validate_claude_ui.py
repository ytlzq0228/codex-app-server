"""Offline browser checks for Claude portal flows (requires playwright + jinja2).
Run: python scripts/validate_claude_ui.py
Uses mocked HTTP responses; never contacts a Worker or consumes subscription quota.
"""
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1] / "src/codex_gateway"
env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True)
from codex_gateway.i18n import t
env.globals.update(t=t, lang='CN', html_lang='zh-CN')
debug = env.get_template("shared/debug.html").render(debug_models=[
    {"id": "gpt-test", "provider": "codex"},
    {"id": "claude-test", "provider": "claude"},
])
login = env.get_template("shared/gemini-login.html").render()
html = """<!doctype html><html lang="zh-CN"><head><script src="/static/i18n.js"></script><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/admin.css"><link rel="stylesheet" href="/static/portal.css">
<link rel="stylesheet" href="/static/modals.css"><script src="/static/modals.js" defer></script>
</head><body><main class="content">""" + debug + """
<form data-provider-login data-provider="claude" data-worker-id="test-worker">
<input name="csrf_token" value="test-csrf" type="hidden"><button>登录 Claude</button>
</form>""" + login + env.get_template("shared/request-dialog.html").render() + "</main></body></html>"

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors, requests = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    state = {"starts": 0, "input": False, "expired": False, "verified": False}

    def route(request):
        path = request.request.url.split("https://gateway.test", 1)[-1]
        if path.startswith("/static/"):
            name = path.removeprefix("/static/").split("?")[0]
            file = ROOT / "static" / name
            request.fulfill(path=str(file), content_type="text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8")
        elif path == "/":
            request.fulfill(body=html, content_type="text/html")
        elif "/provider-login/" in path:
            action = path.rsplit("/", 1)[-1]
            if action == "start":
                state["starts"] += 1
                state["input"] = False
                request.fulfill(json={"session_id": str(state["starts"]), "stage": "authorize",
                                      "expires_in": 120, "login_url": "https://claude.com/cai/oauth/authorize?test=1"})
            elif action == "input":
                if 'name="key"\r\n\r\ncancel' in request.request.post_data:
                    request.fulfill(status=409, json={"detail": "Login is not active"})
                else:
                    state["input"] = True
                    request.fulfill(json={"message": "已提交"})
            elif state["expired"]:
                request.fulfill(json={"stage": "waiting", "error": "授权已过期", "expires_in": 0})
            elif state["input"]:
                request.fulfill(json={"stage": "done", "logged_in": True,
                                      "account": {"email": "claude@example.test", "planType": "team"},
                                      "verification": {"ok": False, "message": "暂时无法推理"}})
            else:
                request.fulfill(json={"stage": "authorize", "expires_in": 90,
                                      "login_url": "https://claude.com/cai/oauth/authorize?test=1"})
        elif path.endswith("/probe"):
            state["verified"] = True
            request.fulfill(json={"ok": True, "message": "Claude 账号和模型访问检查通过"})
        else:
            requests.append(request.request)
            request.fulfill(body='event: message_stop\ndata: {"type":"message_stop"}\n\n',
                            headers={"content-type": "text/event-stream", "x-gateway-generation-policy": "worker-defaults"})
    page.route("https://gateway.test/**", route)
    page.goto("https://gateway.test/")
    page.locator('[name=key]').fill("cag-test")
    page.locator('[name=endpoint]').select_option("/v1/messages")
    assert page.locator('[name=model]').input_value() == "claude-test"
    assert page.locator('[name=model] option[value=gpt-test]').evaluate('(option) => option.disabled && option.hidden')
    payload = json.loads(page.locator('[name=body]').input_value())
    assert payload["model"] == "claude-test" and payload["max_tokens"] == 1024
    page.locator('[name=stream]').check()
    assert json.loads(page.locator('[name=body]').input_value())["stream"] is True
    page.locator('#debug-form button[type=submit]').click()
    expect(page.locator('#debug-status')).to_contain_text("HTTP 200")
    expect(page.locator('#debug-output')).to_contain_text("message_stop")
    expect(page.locator('#debug-headers')).to_contain_text("worker-defaults")
    assert requests[-1].headers["anthropic-version"] == "2023-06-01"
    assert requests[-1].headers["authorization"] == "Bearer cag-test"
    page.locator('[name=endpoint]').select_option("/v1/messages/count_tokens")
    assert "max_tokens" not in json.loads(page.locator('[name=body]').input_value())
    assert "stream" not in json.loads(page.locator('[name=body]').input_value())
    page.locator('[name=body]').fill('{"model":"custom-edit"}')
    page.locator('[name=endpoint]').select_option("/v1/chat/completions")
    assert page.locator('[name=body]').input_value() == '{"model":"custom-edit"}'
    page.locator('[name=endpoint]').select_option("/v1/models")
    page.locator('#debug-form button[type=submit]').click()
    expect(page.locator('#debug-stop')).to_be_disabled()
    assert requests[-1].method == "GET" and requests[-1].post_data is None
    page.locator('[name=endpoint]').select_option("/v1/messages")
    page.locator('[name=body]').fill("invalid JSON")
    count = len(requests)
    page.locator('#debug-form button[type=submit]').click()
    expect(page.locator('#debug-stop')).to_be_disabled()
    assert len(requests) == count
    page.get_by_role("button", name="登录 Claude", exact=True).click()
    expect(page.locator('#gemini-authorization')).to_be_visible()
    assert page.locator('#gemini-login-link').get_attribute("href").startswith("https://claude.com/")
    page.locator('#gemini-auth-code').fill("test-code")
    page.locator('#gemini-code-form button').click()
    expect(page.locator('#gemini-login-account')).to_contain_text("claude@example.test")
    expect(page.locator('#provider-login-retry')).to_have_text("重新探测账号")
    page.locator('#provider-login-retry').click()
    expect(page.locator('#gemini-login-title')).to_have_text("登录成功，推理测试通过")
    assert state["verified"]
    page.screenshot(path="/tmp/claude-portal-login-desktop.png", full_page=True)
    # Reload our fixture, then exercise expired sessions including cancel=409.
    page.reload()
    page.get_by_role("button", name="登录 Claude", exact=True).click()
    expect(page.locator('#gemini-authorization')).to_be_visible()
    state["expired"] = True
    expect(page.locator('#provider-login-retry')).to_have_text("重新开始登录")
    state["expired"] = False
    before = state["starts"]
    page.locator('#provider-login-retry').click()
    expect(page.locator('#gemini-authorization')).to_be_visible()
    assert state["starts"] == before + 1
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path="/tmp/claude-portal-login-mobile.png", full_page=True)
    assert page.locator("#gemini-login-dialog").evaluate("(e) => e.getBoundingClientRect().width") <= 390
    page.reload()
    page.locator('[name=endpoint]').select_option("/v1/messages")
    page.screenshot(path="/tmp/claude-portal-debug-mobile.png", full_page=True)
    assert not errors, errors
    browser.close()
print("PASS Claude debug presets, native headers, streaming, permissions, invalid JSON, login, probe retry, expiry recovery, mobile layout")
