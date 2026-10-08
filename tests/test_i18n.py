import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_gateway.i18n import CATALOG, LanguageMiddleware, current_lang, get_t, resolve_lang, t


@pytest.mark.parametrize("cookie,header,expected", [
    ("CN", "en", "CN"), ("en-US", "zh-CN", "EN"),
    (None, "en;q=0.3,zh-CN;q=0.9", "CN"),
    ("invalid", "zh-TW, en;q=0.8", "CN"),
    (None, "zh;q=0,en;q=0.5", "EN"),
    (None, "fr-FR", "EN"), (None, "zh;q=oops,en;q=0.4", "EN"),
])
def test_language_resolution(cookie, header, expected):
    assert resolve_lang(cookie, header) == expected


def test_catalog_complete_and_interpolation():
    assert CATALOG
    for name, entry in CATALOG.items():
        assert entry["CN"] and entry["EN"], name
        assert set(re.findall(r"\{(\w+)\}", entry["CN"])) == set(re.findall(r"\{(\w+)\}", entry["EN"])), name
        assert not re.search(r"[\u4e00-\u9fff]", entry["EN"]), name
    assert t("登录", "en-US") == "Sign in"
    assert t("登录", "zh-CN") == "登录"
    assert t("登录", "unknown") == "Sign in"
    assert t("missing.message", "EN") == "missing.message"
    assert t("编辑 {worker} 的名称", "EN", worker="<script>") == "Edit name of <script>"
    assert get_t("CN")["语言"] == "语言"


@pytest.mark.asyncio
async def test_concurrent_request_language_isolation():
    app = FastAPI()
    app.add_middleware(LanguageMiddleware)

    @app.get("/")
    async def index():
        before = t("登录")
        await asyncio.sleep(0)
        return {"before": before, "after": t("登录")}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as client:
        en, cn = await asyncio.gather(
            client.get("/", headers={"Accept-Language": "en"}),
            client.get("/", headers={"Accept-Language": "zh-CN"}),
        )
    assert en.json() == {"before": "Sign in", "after": "Sign in"}
    assert cn.json() == {"before": "登录", "after": "登录"}
    assert en.headers["content-language"] == "en"
    assert cn.headers["content-language"] == "zh-CN"
    assert {"cookie", "accept-language"} <= {v.strip().lower() for v in en.headers["vary"].split(",")}
    assert current_lang.get() == "CN"


def test_catalog_js_and_python_match():
    import subprocess
    script = """
const fs=require('fs'),vm=require('vm');
global.window=global;
global.document={cookie:'lang=EN',documentElement:{lang:'zh-CN'},addEventListener(){}};
const catalog=JSON.parse(fs.readFileSync('src/codex_gateway/static/messages.json','utf8'));
global.fetch=async()=>({ok:true,json:async()=>catalog});
vm.runInThisContext(fs.readFileSync('src/codex_gateway/static/i18n.js','utf8'));
(async()=>{await I18n.ready;process.stdout.write(JSON.stringify(
  Object.fromEntries(Object.keys(catalog).map(name=>[name,[I18n.t(name,'CN'),I18n.t(name,'EN')]]))
));})().catch(e=>{console.error(e);process.exit(1)});
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {name: [t(name, "CN"), t(name, "EN")] for name in CATALOG}


def test_catalog_failure_keeps_browser_initialization_available():
    import subprocess
    script = """
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
global.window=global;
global.document={cookie:'lang=EN',documentElement:{lang:'en'},addEventListener(){}};
global.fetch=async()=>{throw new Error('offline')};
vm.runInThisContext(fs.readFileSync('src/codex_gateway/static/i18n.js','utf8'));
(async()=>{await I18n.ready;assert.equal(I18n.t('message {value}','EN',{value:3}),'message 3');})()
  .catch(error=>{console.error(error);process.exit(1)});
"""
    subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)


def test_bilingual_pages_and_branding():
    from codex_gateway.main import app
    from page_helpers import rendered_pages
    from test_self_service import admin_login

    with TestClient(app) as client:
        en = client.get("/auth/login", headers={"Accept-Language": "en"})
        assert '<html lang="en">' in en.text and "Welcome back" in en.text
        assert "Subscription Gateway" in en.text and '/static/logo.svg' in en.text
        assert 'data-language-switch' in en.text
        client.cookies.set("lang", "CN")
        cn = client.get("/auth/login", headers={"Accept-Language": "en"})
        assert '<html lang="zh-CN">' in cn.text and "欢迎回来" in cn.text
        admin_login(client)
        for lang in ("EN", "CN"):
            client.cookies.set("lang", lang)
            for path in ("/admin", "/admin/api-keys", "/admin/workers", "/admin/sessions",
                         "/admin/users", "/admin/finance", "/admin/reports", "/admin/google",
                         "/user/overview", "/user/account", "/user/workers", "/user/debug"):
                page = rendered_pages(client, path)
                assert page.status_code == 200, (path, page.text[:300])
                assert "Subscription Gateway" in page.text, path
                assert "Codex Gateway" not in page.text
                assert ("My account" if lang == "EN" else "我的账户") in page.text, path
            error = client.get("/admin/api-keys/data?page=0")
            assert error.json()["error"]["message"] == t("页码必须为正整数", lang)
        logo = client.get("/static/logo.svg")
        assert logo.status_code == 200 and 'image/svg+xml' in logo.headers["content-type"]
