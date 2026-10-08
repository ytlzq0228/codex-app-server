"""Offline browser regression checks using isolated JSON fixtures."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace as S
from urllib.parse import urlparse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright
from codex_gateway.page_data import shell_context
from codex_gateway.display import money, tokens
ROOT=Path(__file__).resolve().parents[1] / "src/codex_gateway"
def run(fixtures, lang="CN"):
    env=Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
    from codex_gateway.i18n import t
    env.globals.update(t=lambda name, **params: t(name, lang, **params),
                       lang=lang, html_lang='zh-CN' if lang == 'CN' else 'en')
    env.filters.update(money=money, tokens=tokens)
    errors=[]
    fixtures["/user/usage/fixture-request"] = {
        "page": "detail", "identity": fixtures["/user/account"]["identity"],
        "csrf_token": "fixture", "record": {"request_id": "fixture-request",
        "model": "fixture-model", "created_at": "2026-10-03T00:00:00Z",
        "input_tokens": 1250000, "output_tokens": 5, "duration_ms": 3},
        "evidence_fields": [], "observation_fields": [], "last_texts": {},
        "show_worker": True,
    }
    fixtures["/admin"]["stats"]["input_tokens"]=1250000
    fixtures["/admin/reports"]["total"]["input_tokens"]=1250000
    for group in fixtures["/user/usage"]["history"]["groups"]:
        group["input_tokens"]="900000"
        group["output_tokens"]="350000"
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True, args=["--no-sandbox"])
        for path,data in fixtures.items():
            page=browser.new_page(viewport={"width":1440,"height":1000})
            current=[]
            page.on("pageerror", lambda error: current.append(str(error)))
            request=S(url=S(path=path), query_params={}, base_url="https://gateway.test/", state=S(user=S(**data["identity"])))
            shell=env.get_template("data-page.html").render(request=request, **shell_context(data["page"]),
                page_template="admin/dashboard.html" if data["page"] in ["overview","keys","admin_workers","sessions"] else "account.html",
                identity=S(**data["identity"]), csrf_token=data["csrf_token"])
            pending=[]
            def route(r):
                url=urlparse(r.request.url)
                if url.path.startswith("/static/"):
                    file=ROOT / url.path[1:]
                    kind="application/json" if file.suffix==".json" else "text/javascript" if file.suffix==".js" else "text/css"
                    return r.fulfill(body=file.read_bytes(),content_type=kind)
                if url.path==path: return r.fulfill(body=shell,content_type="text/html")
                if url.path==path+"/data":
                    pending.append(r)
                    return
                return r.fulfill(status=503,json={"detail":"offline fixture"})
            page.route("https://gateway.test/**", route)
            page.goto("https://gateway.test"+path, wait_until="domcontentloaded")
            page.wait_for_function("document.querySelector('main[data-page-loading]')")
            assert page.locator("main h1").is_visible(), path
            assert "正在加载页面数据" not in page.locator("main").inner_text()
            assert page.locator("main section").count(), path
            # Fail the first data fetch: the layout must stay visible and retry work.
            while not pending: page.wait_for_timeout(20)
            pending.pop().fulfill(status=503, json={"detail":"测试加载失败"})
            page.locator("[data-page-error]:visible").wait_for()
            assert page.locator("main h1").is_visible()
            page.locator("[data-page-retry]").click()
            while not pending: page.wait_for_timeout(20)
            pending.pop().fulfill(json=data)
            page.wait_for_function("!document.querySelector('main').hasAttribute('aria-busy')")
            page.wait_for_timeout(150)
            assert page.evaluate("TokenFormat.tokens(999999)") == "999999"
            assert page.evaluate("TokenFormat.tokens(1000000)") == "1000000"
            assert page.evaluate("TokenFormat.tokens(1000001)") == "1.0000 million"
            assert page.evaluate("TokenFormat.tokens(1250000)") == "1.2500 million"
            assert page.evaluate("TokenFormat.total('900000','350000')") == "1.2500 million"
            if data["page"]=="overview":
                assert "1.2500 million" in page.locator("#overview").inner_text()
            if data["page"]=="reports":
                assert "1.2500 million" in page.locator("main").inner_text()
            if data["page"]=="usage":
                page.get_by_text("1.2500 million", exact=True).first.wait_for()
            trigger=page.locator("[data-open-dialog], [data-modal-open]").first
            if trigger.count() and trigger.is_enabled():
                trigger.click()
                assert page.locator("dialog[open]").count() == 1
                page.keyboard.press("Escape")
            error=page.locator("[data-page-error]:visible")
            if error.count(): current.append(error.inner_text())
            if not page.locator("main h1").count(): current.append("Missing page title")
            print(path, current or "ok",flush=True)
            errors += [(path,x) for x in current]
            page.close()
        browser.close()
    assert not errors,errors
if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("fixtures",type=Path)
    parser.add_argument("--lang", choices=["CN", "EN"], default="CN")
    args=parser.parse_args()
    run(json.loads(args.fixtures.read_text()), args.lang)
