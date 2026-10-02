"""Prune only old, unbound Claude transcripts; active bindings never expire."""
import asyncio
import logging

from sqlalchemy import select

from .config import get_settings
from .database import SessionLocal
from .gemini_backend import worker_rpc
from .models import ExecutionSession, ResponseBinding, Worker


async def prune_sessions():
    async with SessionLocal() as db:
        workers = (await db.execute(select(Worker.id, Worker.endpoint).where(
            Worker.provider == "claude", Worker.endpoint != "removed://worker"))).all()
        retained = {}
        for worker_id, thread in await db.execute(select(ResponseBinding.worker_id, ResponseBinding.thread_id).where(
                ResponseBinding.provider == "claude", ResponseBinding.status == "active")):
            retained.setdefault(worker_id, set()).add(thread)
        for worker_id, thread in await db.execute(select(ExecutionSession.worker_id, ExecutionSession.thread_id).where(
                ExecutionSession.provider == "claude", ExecutionSession.state != "invalid", ExecutionSession.thread_id.is_not(None))):
            retained.setdefault(worker_id, set()).add(thread)
    for worker_id, endpoint in workers:
        try:
            await worker_rpc(endpoint, get_settings(), "/sessions/prune",
                             {"keep_ids": sorted(retained.get(worker_id, ())), "min_age_seconds": 86400})
        except Exception:
            logging.getLogger(__name__).warning("Claude session cleanup deferred: worker_id=%s", worker_id)


async def session_cleanup_loop():
    while True:
        try:
            await prune_sessions()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception("Claude session cleanup failed")
        await asyncio.sleep(3600)
