"""Shared placement and owner execution. HTTP ingress has no locality preference."""
import asyncio
import hmac
import json
import logging
from dataclasses import asdict
from datetime import timedelta
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert

from .backend import BackendTarget, WorkerFailure
from .client_tools import ToolProtocolError, tool_outputs
from .config import get_settings
from .database import SessionLocal
from .models import AppNode, PendingToolRoute, Worker
from .schemas import BackendResult, BackendStreamEvent, ResponseRequest

router = APIRouter()


def cutoff(settings):
    return func.now() - text(f"INTERVAL '{int(settings.node_timeout_seconds)} seconds'")


async def manager_for(db, worker, settings):
    if not settings.node_id:
        return settings.manager_url
    if not worker.node_id:
        raise HTTPException(503, "Worker has no node assignment")
    node = await db.get(AppNode, worker.node_id)
    if not node:
        raise HTTPException(503, "Worker node is unavailable")
    return node.manager_url


async def select_node(db, settings):
    if not settings.node_id:
        return None, settings.manager_url
    # Held through the Worker insert/manager operation/commit. All creation paths
    # take this lock, so in-flight creations count before the next choice.
    await db.execute(text("SELECT pg_advisory_xact_lock(719342001)"))
    nodes = (await db.scalars(select(AppNode).where(
        AppNode.enabled.is_(True), AppNode.heartbeat_at > cutoff(settings)))).all()
    if not nodes:
        raise HTTPException(503, "No healthy application node is available")
    counts = dict((await db.execute(select(Worker.node_id, func.count()).where(
        Worker.endpoint != "removed://worker").group_by(Worker.node_id))).all())
    node = min(nodes, key=lambda node: (counts.get(node.id, 0), sum(node.active_connections.values()), node.id))
    return node.id, node.manager_url


async def live_nodes(db, settings):
    return (await db.scalars(select(AppNode).where(
        AppNode.enabled.is_(True), AppNode.heartbeat_at > cutoff(settings)))).all()


async def heartbeat_loop(backend, settings):
    while True:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(settings.node_manager_url + "/healthz")
                response.raise_for_status()
            active = await backend.pool.active_connections_by_worker()
            async with SessionLocal() as db:
                statement = insert(AppNode).values(id=settings.node_id,
                    gateway_url=settings.node_gateway_url, manager_url=settings.node_manager_url,
                    heartbeat_at=func.now(), active_connections=active)
                await db.execute(statement.on_conflict_do_update(index_elements=["id"], set_={
                    "gateway_url": settings.node_gateway_url, "manager_url": settings.node_manager_url,
                    "heartbeat_at": func.now(), "active_connections": active}))
                await db.execute(delete(PendingToolRoute).where(PendingToolRoute.expires_at <= func.now()))
                await db.commit()
            await reconcile_provisioning(settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception("Node heartbeat failed")
        await asyncio.sleep(5)


async def provision(db, worker, settings, manager_url):
    """Reserve placement durably before Docker: a lost reply cannot orphan identity."""
    worker.endpoint = "provisioning://worker"
    await db.commit()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(manager_url + "/workers",
                json={"name": worker.container_name, "provider": worker.provider},
                headers={"Authorization": "Bearer " + settings.manager_token.get_secret_value()})
            response.raise_for_status()
        worker.endpoint = response.json()["endpoint"]
        await db.commit()
    except httpx.HTTPError as exc:
        # The reservation is kept; heartbeat retries the same container name.
        raise HTTPException(503, "Worker provisioning is pending; retry status shortly") from exc
    return worker


async def reconcile_provisioning(settings):
    async with SessionLocal() as db:
        workers = (await db.scalars(select(Worker).where(Worker.node_id == settings.node_id,
            Worker.endpoint == "provisioning://worker").limit(1).with_for_update(skip_locked=True))).all()
        for worker in workers:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    response = await client.post(settings.node_manager_url + "/workers",
                        json={"name": worker.container_name, "provider": worker.provider},
                        headers={"Authorization": "Bearer " + settings.manager_token.get_secret_value()})
                    response.raise_for_status()
                worker.endpoint = response.json()["endpoint"]
            except httpx.HTTPError:
                continue
        await db.commit()


async def publish_tool(run, ttl):
    settings = get_settings()
    if not settings.node_id:
        return
    target = asdict(run.target)
    if target["worker_id"]:
        target["worker_id"] = str(target["worker_id"])
    async with SessionLocal() as db:
        statement = insert(PendingToolRoute).values(
            key_id=run.target.connection_key.split(":", 1)[0], call_id=run.call_id,
            node_id=settings.node_id, thread_id=run.thread_id, target=target,
            expires_at=func.now() + text(f"INTERVAL '{int(ttl)} seconds'"))
        await db.execute(statement.on_conflict_do_update(
            index_elements=["key_id", "call_id"], set_={
                "thread_id": run.thread_id, "target": target,
                "expires_at": statement.excluded.expires_at, "node_id": settings.node_id}))
        await db.commit()


async def pending_route(request, key, backend):
    settings = get_settings()
    if settings.node_id and tool_outputs(request):
        outputs = tool_outputs(request)
        if len(outputs) != 1:
            raise ToolProtocolError("Return exactly one pending client tool output at a time")
        async with SessionLocal() as db:
            row = await db.get(PendingToolRoute, (str(key or "development"), outputs[0][0]))
            valid = row and await db.scalar(select(PendingToolRoute.call_id).where(
                PendingToolRoute.key_id == row.key_id, PendingToolRoute.call_id == row.call_id,
                PendingToolRoute.expires_at > func.now()))
            if valid and row.node_id != settings.node_id:
                data = dict(row.target)
                data["worker_id"] = UUID(data["worker_id"]) if data["worker_id"] else None
                return BackendTarget(**data), row.thread_id
    target = backend.continuation_target(request, key) if hasattr(backend, "continuation_target") else None
    thread = backend.continuation_thread(request, key) if target and hasattr(backend, "continuation_thread") else None
    return target, thread


async def owner_url(target, settings):
    if not settings.node_id or not target.worker_id:
        return None
    async with SessionLocal() as db:
        worker = await db.get(Worker, target.worker_id)
        if not worker or not worker.node_id:
            raise WorkerFailure("Worker has no node assignment", safe_to_retry=True)
        node = await db.scalar(select(AppNode).where(AppNode.id == worker.node_id,
            AppNode.enabled.is_(True), AppNode.heartbeat_at > cutoff(settings)))
        if not node:
            raise WorkerFailure("Worker node is unavailable", safe_to_retry=True)
        return None if worker.node_id == settings.node_id else node.gateway_url


async def remote_stream(url, request, target, settings):
    data = asdict(target)
    if data["worker_id"]:
        data["worker_id"] = str(data["worker_id"])
    payload = {"request": request.model_dump(mode="json"), "target": data,
               "execution_input_text": request._execution_input_text,
               "execution_input_items": request._execution_input_items,
               "execution_auto_resume": request._execution_auto_resume}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.app_server_timeout_seconds, connect=5)) as client:
            async with client.stream("POST", url + "/internal/execution", json=payload,
                headers={"Authorization": "Bearer " + settings.manager_token.get_secret_value()}) as response:
                response.raise_for_status()
                terminal = False
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    if "error" in data:
                        error = data["error"]
                        if error.get("code"):
                            raise ToolProtocolError(error["message"], error["code"])
                        if error.get("capacity"):
                            from .app_server import AppServerCapacityError
                            raise AppServerCapacityError(error["message"])
                        raise WorkerFailure(error["message"], kind=error["kind"], safe_to_retry=error["safe_to_retry"])
                    event = BackendStreamEvent.model_validate(data)
                    from .usage_accounting import observe_usage
                    observe_usage(target, event)
                    terminal = bool(event.done or event.tool_call)
                    yield event
                if not terminal:
                    raise WorkerFailure("Remote execution ended without a final result")
    except httpx.HTTPError as exc:
        # A timeout/EOF may follow turn/start. Never replay automatically.
        raise WorkerFailure("Application node connection failed", safe_to_retry=False) from exc


async def collect(events):
    text_parts, calls, last = [], [], None
    async for event in events:
        last = event
        text_parts.append(event.delta)
        if event.tool_call:
            calls.append(event.tool_call)
    if last is None or not (last.done or calls):
        raise WorkerFailure("Execution returned no final result")
    return BackendResult(text="".join(text_parts), tool_calls=calls,
        usage_accounting=last.usage_accounting,
        thread_id=last.thread_id, input_tokens=last.input_tokens, output_tokens=last.output_tokens,
        cache_read_tokens=last.cache_read_tokens, cache_write_tokens=last.cache_write_tokens)


@router.post("/internal/execution", include_in_schema=False)
async def execute(request: Request):
    settings = get_settings()
    expected = "Bearer " + settings.manager_token.get_secret_value()
    if not settings.node_id or not hmac.compare_digest(request.headers.get("authorization", ""), expected):
        raise HTTPException(401, "Unauthorized")
    payload = await request.json()
    data = payload["target"]
    data["worker_id"] = UUID(data["worker_id"])
    target = BackendTarget(**data)
    async with SessionLocal() as db:
        worker = await db.get(Worker, target.worker_id)
        if (not worker or worker.node_id != settings.node_id or worker.endpoint != target.endpoint
                or worker.execution_generation != target.worker_generation or worker.provider != target.provider
                or worker.endpoint == "removed://worker" or not worker.enabled):
            raise HTTPException(409, "Worker identity changed")
    body = ResponseRequest.model_validate(payload["request"])
    body._execution_input_text = payload.get("execution_input_text")
    body._execution_input_items = payload.get("execution_input_items")
    body._execution_auto_resume = payload.get("execution_auto_resume", False)
    async def events():
        from contextlib import aclosing
        try:
            async with aclosing(request.app.state.backend.stream(body, target)) as stream:
                async for event in stream:
                    yield event.model_dump_json() + "\n"
        except ToolProtocolError as exc:
            yield json.dumps({"error": {"message": str(exc), "code": exc.code}}) + "\n"
        except Exception as exc:
            from .app_server import AppServerCapacityError
            failure = exc if isinstance(exc, WorkerFailure) else WorkerFailure("Worker execution failed")
            yield json.dumps({"error": {"message": str(failure), "kind": failure.kind,
                "safe_to_retry": failure.safe_to_retry, "capacity": isinstance(exc, AppServerCapacityError)}}) + "\n"
    return StreamingResponse(events(), media_type="application/x-ndjson")
