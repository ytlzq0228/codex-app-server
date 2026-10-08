"""Reusable user messaging and transactional Worker failure notifications."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import dchat
from .models import DChatConfig, User, Worker, WorkerNotification, WorkerStatus

logger = logging.getLogger(__name__)


async def send_message(db, username: str, text: str) -> dict:
    """Common application entry point for sending a user a message."""
    config = await db.get(DChatConfig, 1)
    return await dchat.send_text_message(config, username, text)


def needs_attention(worker):
    return bool(worker.enabled and worker.owner_username
                and worker.endpoint != "removed://worker"
                and worker.status == WorkerStatus.error)


async def stage_worker_notification(db, worker):
    """Called within reconciliation's quota lock; no network I/O or commit."""
    notice = await db.get(WorkerNotification, worker.id)
    if not needs_attention(worker):
        if notice:
            notice.active = False
        return
    if notice is None:
        db.add(WorkerNotification(worker_id=worker.id, owner_username=worker.owner_username))
    elif not notice.active or notice.owner_username != worker.owner_username:
        notice.active = True
        notice.owner_username = worker.owner_username
        notice.sent_at = notice.retry_at = None


async def deliver_worker_notification(db, worker_id):
    # Lock order matches reconciliation (Worker, then notification). SKIP LOCKED
    # lets multiple gateway processes drain safely without duplicate sends.
    worker = await db.scalar(select(Worker).where(Worker.id == worker_id)
                             .with_for_update(skip_locked=True))
    if worker is None:
        return False
    notice = await db.scalar(select(WorkerNotification).where(
        WorkerNotification.worker_id == worker_id).with_for_update(skip_locked=True))
    now = datetime.now(timezone.utc)
    if notice is None or not notice.active or notice.sent_at:
        return False
    if not needs_attention(worker) or notice.owner_username != worker.owner_username:
        notice.active = False
        await db.commit()
        return False
    if notice.retry_at and notice.retry_at > now:
        return False
    user = await db.get(User, notice.owner_username)
    if not user or not user.enabled:
        notice.retry_at = now + timedelta(minutes=5)
        await db.commit()
        return False
    reasons = {
        "logged_out": "登录已失效，请重新登录订阅账号",
        "limit": "上游用量受限，请检查额度",
        "connection": "服务连接异常，请检查 Worker 状态",
        "ineligible": "账号未通过资格检查，请检查订阅账号",
    }
    reason = reasons.get(worker.failure_kind, "运行异常，请检查 Worker 状态")
    message = (f"【Subscription Gateway】您的 Worker「{worker.name}」"
               f"（{worker.provider or 'codex'}）{reason}。"
               "请返回系统的「贡献 Worker」页面（/user/workers）处理并重新探测。")
    outcome = await send_message(db, notice.owner_username, message)
    if outcome["success"]:
        notice.sent_at = now
        notice.retry_at = None
    else:
        notice.retry_at = now + timedelta(minutes=5)
        logger.warning("Worker notification deferred: worker_id=%s", worker.id)
    await db.commit()
    return outcome["success"]


async def notification_loop():
    from .database import SessionLocal
    while True:
        try:
            async with SessionLocal() as db:
                config = await db.get(DChatConfig, 1)
                ids = []
                if dchat.configured(config):
                    ids = (await db.scalars(select(WorkerNotification.worker_id).where(
                        WorkerNotification.active.is_(True),
                        WorkerNotification.sent_at.is_(None),
                        (WorkerNotification.retry_at.is_(None)
                         | (WorkerNotification.retry_at <= datetime.now(timezone.utc)))
                    ).limit(100))).all()
            for worker_id in ids:
                try:
                    async with SessionLocal() as db:
                        await deliver_worker_notification(db, worker_id)
                except Exception:
                    logger.exception("Worker notification delivery failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker notification loop failed")
        await asyncio.sleep(30)
