"""D-Chat transport and persistent profile cache. Credentials stay on the server."""
import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone

import httpx

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .database import SessionLocal
from .models import DChatConfig, DChatUserCache

logger = logging.getLogger(__name__)
# Fixed regional API endpoint, shared by messaging and user lookup.
BASE_URL = "http://10.14.128.126/snitch_openapi_online_lb"
PROFILE_CACHE_TTL = timedelta(days=7)


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


async def display_names(db, usernames, *, cache_only=False) -> dict[str, str]:
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
    # Cache transactions are independent of the caller's transaction: read-only
    # pages must persist refreshes without committing unrelated caller changes.
    async with SessionLocal() as cache_db:
        cached = (await cache_db.scalars(
            select(DChatUserCache).where(
                DChatUserCache.credential_fingerprint == fingerprint,
                DChatUserCache.username.in_(names),
            )
        )).all()
    fresh = {
        entry.username: entry.profile for entry in cached
        if entry.refreshed_at + PROFILE_CACHE_TTL > datetime.now(timezone.utc)
    }

    def display_name(profile, name):
        display = profile.get("full_name") or profile.get("fullname")
        return (display.strip() if isinstance(display, str) else "") or name

    for name, profile in fresh.items():
        result[name] = display_name(profile, name)

    if cache_only:
        return result

    slots = asyncio.Semaphore(5)
    refreshed = []

    async def resolve(name):
        async with slots:
            profile = await get_user_info(config, name)
        if profile is None:
            return
        result[name] = display_name(profile, name)
        refreshed.append({
            "credential_fingerprint": fingerprint,
            "username": name,
            "profile": profile,
            "refreshed_at": datetime.now(timezone.utc),
        })

    # A slow directory must not turn a paginated admin page into a long queue.
    tasks = [asyncio.create_task(resolve(name)) for name in names if name not in fresh]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=4)
    except TimeoutError:
        pass
    if refreshed:
        # Serialize database work after network lookups; AsyncSession cannot be
        # shared by concurrent tasks. Upsert also handles competing page loads.
        async with SessionLocal() as cache_db:
            statement = insert(DChatUserCache).values(refreshed)
            await cache_db.execute(statement.on_conflict_do_update(
                index_elements=["credential_fingerprint", "username"],
                set_={"profile": statement.excluded.profile,
                      "refreshed_at": statement.excluded.refreshed_at},
                where=DChatUserCache.refreshed_at < statement.excluded.refreshed_at,
            ))
            await cache_db.commit()
    return result
