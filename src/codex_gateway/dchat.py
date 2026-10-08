"""D-Chat transport and bounded profile cache. Credentials stay on the server."""
import asyncio
import hashlib
import logging
import time
from collections import OrderedDict

import httpx

from .models import DChatConfig

logger = logging.getLogger(__name__)
# Fixed regional API endpoint, shared by messaging and user lookup.
BASE_URL = "http://10.14.128.126/snitch_openapi_online_lb"
_profiles = OrderedDict()
CACHE_LIMIT = 2048


def configured(config):
    return bool(config and config.enabled and config.bot_id
                and config.api_client_id and config.api_client_secret)


def api_success(data):
    if not isinstance(data, dict):
        return False
    return (data.get("success") is not False and data.get("ok") is not False
            and not data.get("error")
            and data.get("errno", 0) in (0, "0", None)
            and data.get("code", 0) in (0, "0", 200, "200", None))


async def send_text_message(config, username: str, text: str) -> dict:
    if not configured(config):
        return {"success": False, "error": "D-Chat is disabled or not configured"}
    if not username.strip() or not text.strip():
        return {"success": False, "error": "Recipient and message are required"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                BASE_URL + "/v1/message.create",
                auth=(config.api_client_id, config.api_client_secret),
                json={"bot_type": "bot_user", "bot_id": config.bot_id,
                      "username": username, "text": text},
                headers={"Content-Type": "application/json;charset=utf-8"},
            )
        if response.status_code != 200:
            return {"success": False, "error": f"HTTP {response.status_code}"}
        # The reference API also permits an empty HTTP 200 response.
        if response.content and not api_success(response.json()):
            return {"success": False, "error": "D-Chat rejected the message"}
        return {"success": True, "error": None}
    except Exception as exc:
        # Never log the response body, URLs with query data or auth credentials.
        logger.warning("D-Chat send failed: %s", type(exc).__name__)
        return {"success": False, "error": "D-Chat request failed"}


async def get_user_info(config, username: str) -> dict | None:
    if not configured(config) or not username:
        return None
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(
                BASE_URL + "/v1/user.info",
                auth=(config.api_client_id, config.api_client_secret),
                params={"bot_type": "bot_user", "bot_id": config.bot_id, "username": username},
            )
        response.raise_for_status()
        data = response.json()
        profile = data.get("result") if api_success(data) else None
        return profile if isinstance(profile, dict) and profile else None
    except Exception as exc:
        logger.warning("D-Chat profile lookup failed: %s", type(exc).__name__)
        return None


async def display_names(db, usernames) -> dict[str, str]:
    names = list(dict.fromkeys(name for name in usernames if name))
    result = {name: name for name in names}
    if not names:
        return result
    config = await db.get(DChatConfig, 1)
    if not configured(config):
        return result
    fingerprint = hashlib.sha256(
        repr((config.bot_id, config.api_client_id, config.api_client_secret)).encode()
    ).hexdigest()
    slots = asyncio.Semaphore(5)

    async def resolve(name):
        key = (fingerprint, name)
        cached = _profiles.get(key)
        if cached and cached[0] > time.monotonic():
            result[name] = cached[1]
            _profiles.move_to_end(key)
            return
        async with slots:
            profile = await get_user_info(config, name)
        display = (profile or {}).get("full_name") or (profile or {}).get("fullname")
        display = display.strip() if isinstance(display, str) else ""
        result[name] = display or name
        _profiles[key] = (time.monotonic() + (1800 if display else 60), result[name])
        _profiles.move_to_end(key)
        while len(_profiles) > CACHE_LIMIT:
            _profiles.popitem(last=False)

    # A slow directory must not turn a paginated admin page into a long queue.
    tasks = [asyncio.create_task(resolve(name)) for name in names]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=4)
    except TimeoutError:
        pass
    return result
