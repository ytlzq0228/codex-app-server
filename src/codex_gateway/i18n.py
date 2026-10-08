"""Shared UI catalog, cookie/browser language selection and named interpolation."""
import json
import re
from contextvars import ContextVar
from pathlib import Path

CATALOG = json.loads((Path(__file__).parent / "static/messages.json").read_text())
LANG_NAMES = {"EN": "English", "CN": "中文"}
current_lang = ContextVar("ui_language", default="CN")

def normalize_lang(lang):
    code = str(lang or "").replace("_", "-").split("-")[0].upper()
    return "CN" if code in {"ZH", "CN"} else "EN" if code == "EN" else None

def resolve_lang(cookie_lang=None, accept_language=None):
    explicit = normalize_lang(cookie_lang)
    if explicit:
        return explicit
    candidates = []
    for index, part in enumerate((accept_language or "").split(",")):
        tag, *params = part.strip().split(";")
        quality = 1.0
        try:
            for param in params:
                if param.strip().startswith("q="):
                    quality = float(param.strip()[2:])
        except ValueError:
            continue
        code = normalize_lang(tag)
        if code and 0 < quality <= 1:
            candidates.append((quality, -index, code))
    return max(candidates)[2] if candidates else "EN"

def t(name, lang=None, **params):
    code = normalize_lang(lang) if lang is not None else current_lang.get()
    entry = CATALOG.get(name, {})
    value = entry.get(code or "EN") or entry.get("EN") or name
    return re.sub(r"\{(\w+)\}", lambda m: str(params.get(m[1], m[0])), value)

def get_t(lang):
    return {name: t(name, lang) for name in CATALOG}

def template_context(request):
    lang = resolve_lang(request.cookies.get("lang"), request.headers.get("accept-language"))
    return {"lang": lang, "html_lang": "zh-CN" if lang == "CN" else "en",
            "t": lambda name, **params: t(name, lang, **params)}

class LanguageMiddleware:
    """Pure ASGI middleware keeps concurrent requests' language choices isolated."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.requests import Request
        request = Request(scope)
        lang = resolve_lang(request.cookies.get("lang"), request.headers.get("accept-language"))
        token = current_lang.set(lang)
        async def localized_send(message):
            if message["type"] == "http.response.start":
                from starlette.datastructures import MutableHeaders
                headers = MutableHeaders(scope=message)
                headers["Content-Language"] = "zh-CN" if lang == "CN" else "en"
                headers.add_vary_header("Cookie")
                headers.add_vary_header("Accept-Language")
            await send(message)
        try:
            await self.app(scope, receive, localized_send)
        finally:
            current_lang.reset(token)
