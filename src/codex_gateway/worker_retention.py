"""Periodically remove expired managed containers and archive active worker records."""
import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from .config import get_settings
from .database import SessionLocal
from .models import Worker, WorkerStatus
from .quota import quota_lock, reconcile_worker
from .retention_policy import RETENTION
from .worker_names import archived_worker_name

log = logging.getLogger(__name__)


async def archive_worker(db, worker):
    from .binding_lifecycle import invalidate_bindings
    worker.execution_generation = (worker.execution_generation or 0) + 1
    await invalidate_bindings(db, worker_id=worker.id, reason="worker_deleted")
    worker.enabled = False
    worker.status = WorkerStatus.offline
    worker.endpoint = "removed://worker"
    names = set((await db.scalars(select(Worker.name).where(Worker.id != worker.id))).all())
    containers = set((await db.scalars(select(Worker.container_name).where(Worker.id != worker.id))).all())
    worker.name = archived_worker_name(worker.name, worker.id, names)
    worker.container_name = archived_worker_name(worker.container_name, worker.id, containers)
    await reconcile_worker(db, worker)


def expired_logout(worker, now):
    return (not worker.auth_mode and worker.logged_out_since is not None
            and worker.logged_out_since <= now - RETENTION)


async def cleanup_once(settings):
    manager_url = settings.node_manager_url if settings.node_id else settings.manager_url
    headers = {"Authorization": "Bearer " + settings.manager_token.get_secret_value()}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        response = await client.get(manager_url + "/workers")
        response.raise_for_status()
        inventory = {row["name"]: row for row in response.json()}
        async with SessionLocal() as db:
            query = select(Worker.id).where(Worker.endpoint.not_in(["removed://worker", "provisioning://worker"]))
            if settings.node_id:
                query = query.where(Worker.node_id == settings.node_id)
            ids = (await db.scalars(query)).all()
        for worker_id in ids:
            try:
                async with SessionLocal() as db:
                    await quota_lock(db)
                    worker = await db.scalar(select(Worker).where(Worker.id == worker_id).with_for_update())
                    if not worker or worker.endpoint in {"removed://worker", "provisioning://worker"}:
                        continue
                    row = inventory.pop(worker.container_name, None)
                    if row is None:
                        continue
                    now = datetime.now(timezone.utc)
                    stopped = datetime.fromisoformat(row["stopped_since"]) if row and row.get("stopped_since") else None
                    stop_expired = stopped is not None and stopped < now - RETENTION
                    # Existing installations start their unobserved history now.
                    # A missing auth_mode alone is not proof of a failed login.
                    if not stop_expired and (expired_logout(worker, now) or worker.logged_out_since is None):
                        if not worker.auth_mode and row and row["state"] == "running":
                            from .admin import probe_worker_record
                            probe = await probe_worker_record(worker, db, settings)
                            if probe and probe.get("busy"):
                                continue
                            # Probes may commit; reacquire the same lock order before deletion.
                            await quota_lock(db)
                            worker = await db.scalar(select(Worker).where(Worker.id == worker_id)
                                .with_for_update().execution_options(populate_existing=True))
                            if worker.endpoint in {"removed://worker", "provisioning://worker"}:
                                continue
                            if not worker.auth_mode and worker.failure_kind != "logged_out":
                                # A timeout cannot establish that credentials are absent.
                                await db.commit()
                                continue
                    if not (stop_expired or expired_logout(worker, now)):
                        await db.commit()
                        continue
                    params = {"expected_id": row["id"]} if row else {}
                    if stop_expired:
                        params["stopped_before"] = (now - RETENTION).isoformat()
                    result = await client.delete(manager_url + "/workers/" + worker.container_name, params=params)
                    if result.status_code not in {204, 404}:
                        result.raise_for_status()
                        raise ValueError("Unexpected worker deletion response")
                    await archive_worker(db, worker)
                    await db.commit()
                    log.info("Worker retention removed worker_id=%s reason=%s", worker_id,
                             "stopped" if stop_expired else "logged_out")
            except Exception:
                log.exception("Worker retention failed for worker_id=%s", worker_id)
        # Upgrade backups have no current database record. Match by actual Docker
        # name, never the inherited worker label, and keep shared named volumes.
        for name, row in inventory.items():
            if not row.get("stopped_since"):
                continue
            cutoff = datetime.now(timezone.utc) - RETENTION
            if datetime.fromisoformat(row["stopped_since"]) >= cutoff:
                continue
            try:
                async with SessionLocal() as db:
                    await quota_lock(db)
                    registered = await db.scalar(select(Worker.id).where(Worker.container_name == name,
                        Worker.endpoint != "removed://worker"))
                    if registered:
                        continue
                    result = await client.delete(manager_url + "/workers/" + name,
                        params={"expected_id": row["id"], "stopped_before": cutoff.isoformat()})
                    if result.status_code != 404:
                        result.raise_for_status()
                    log.info("Worker retention removed stopped backup container_id=%s", row["id"])
            except Exception:
                log.exception("Stopped backup cleanup failed container_id=%s", row["id"])


async def worker_retention_loop():
    while True:
        try:
            await cleanup_once(get_settings())
        except Exception:
            log.exception("Worker retention sweep failed")
        await asyncio.sleep(3600)
