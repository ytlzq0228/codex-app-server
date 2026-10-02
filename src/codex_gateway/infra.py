"""Administrator-only status for the registered APP nodes and their Docker hosts."""
import asyncio
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .admin_auth import AdminSession, require_admin
from .config import get_settings
from .database import get_session
from .models import AppNode, Worker

router = APIRouter(prefix="/infra")


@router.get("/")
async def page(request: Request, admin: AdminSession = Depends(require_admin)):
    return request.app.state.templates.TemplateResponse(request, "admin/infra.html", {"page": "infra", "csrf_token": admin.csrf_token})


async def probe_node(client, node, token):
    result = {key: value for key, value in node.items() if key not in {"manager_url", "gateway_url"}}
    async def gateway():
        try:
            response = await client.get(node["gateway_url"].rstrip("/") + "/healthz", timeout=3)
            result["gateway_status"] = "online" if response.status_code == 200 else "offline"
        except httpx.HTTPError:
            result["gateway_status"] = "offline"
    async def docker():
        try:
            response = await client.get(node["manager_url"].rstrip("/") + "/infra", headers={"Authorization": "Bearer " + token}, timeout=8)
            response.raise_for_status()
            result["docker"] = response.json()
            result["manager_status"] = "online"
        except (httpx.HTTPError, ValueError):
            result["manager_status"] = "offline"
            result["docker"] = None
    await asyncio.gather(gateway(), docker())
    return result


@router.get("/status")
async def status(admin: AdminSession = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    now = datetime.now(timezone.utc)
    registered = (await session.scalars(select(AppNode).order_by(AppNode.id))).all() if settings.node_id else []
    workers = (await session.scalars(select(Worker).where(Worker.endpoint != "removed://worker"))).all()
    nodes = [{"id": node.id, "address": urlsplit(node.gateway_url).hostname,
              "gateway_url": node.gateway_url, "manager_url": node.manager_url, "enabled": node.enabled,
              "heartbeat_at": node.heartbeat_at.isoformat(),
              "heartbeat_fresh": (now - node.heartbeat_at).total_seconds() <= settings.node_timeout_seconds,
              "connections": sum(node.active_connections.values()),
              "workers": [{"name": worker.name, "container_name": worker.container_name, "status": worker.status.value} for worker in workers if worker.node_id == node.id]}
             for node in registered]
    if not settings.node_id:
        nodes = [{"id": "本机", "address": "当前 APP", "gateway_url": "http://127.0.0.1:8000", "manager_url": settings.manager_url,
                  "enabled": True, "heartbeat_at": None, "heartbeat_fresh": None, "connections": None,
                  "workers": [{"name": worker.name, "container_name": worker.container_name, "status": worker.status.value} for worker in workers]}]
    db = dict((await session.execute(text("SELECT current_database() AS name, inet_server_addr()::text AS address, pg_is_in_recovery() AS replica, pg_database_size(current_database()) AS size_bytes"))).mappings().one())
    await session.rollback()  # Do not hold a database connection during Docker sampling.
    async with httpx.AsyncClient() as client:
        snapshots = await asyncio.gather(*(probe_node(client, node, settings.manager_token.get_secret_value()) for node in nodes))
    return JSONResponse({"timestamp": now.isoformat(), "current_node": settings.node_id or "本机", "database": db, "nodes": snapshots}, headers={"Cache-Control": "no-store"})
