import hmac
import os
import re
from datetime import datetime, timezone

import docker
from docker.errors import APIError, NotFound
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from typing import Literal

app = FastAPI(title="Codex Worker Manager", docs_url=None, redoc_url=None, openapi_url=None)
client = docker.from_env()
MANAGED_LABEL = "io.codex-gateway.managed"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
ID_RE = re.compile(r"^[a-zA-Z0-9-]{1,80}$")
WORKSPACE_SLOT_COUNT = int(os.environ.get("CODEX_MAX_WS_PER_KEY_WORKER", "10"))


class WorkerSpec(BaseModel):
    name: str
    provider: Literal["codex", "gemini", "claude"] = "codex"


def authorize(authorization: str | None) -> None:
    expected = os.environ.get("CODEX_MANAGER_TOKEN", "")
    # The manager controls docker.sock: a missing or placeholder token must never authorize.
    lowered = expected.strip().lower()
    if len(lowered) < 16 or any(marker in lowered for marker in ("change-me", "change-this", "changeme")):
        raise HTTPException(503, "manager token is not configured")
    if not hmac.compare_digest(authorization or "", f"Bearer {expected}"):
        raise HTTPException(401, "unauthorized")


def managed_container(name: str):
    try:
        container = client.containers.get(name)
    except NotFound as exc:
        raise HTTPException(404, "worker not found") from exc
    if container.labels.get(MANAGED_LABEL) != "true":
        raise HTTPException(403, "container is not managed by codex-gateway")
    return container


@app.get("/healthz")
def health() -> dict[str, str]:
    client.ping()
    return {"status": "ok"}


@app.post("/workers", status_code=201)
def create_worker(spec: WorkerSpec, authorization: str | None = Header(default=None)) -> dict[str, str]:
    authorize(authorization)
    if not NAME_RE.fullmatch(spec.name):
        raise HTTPException(400, "invalid worker name")
    if spec.provider == "claude":
        image = os.environ.get("CLAUDE_WORKER_IMAGE", "codex-claude-worker:2.1.287")
    elif spec.provider == "gemini":
        image = os.environ.get("GEMINI_WORKER_IMAGE", "codex-antigravity-worker:1.2.12")
    else:
        image = os.environ["CODEX_WORKER_IMAGE"]
    home = {"claude": "/home/claude", "gemini": "/home/agy", "codex": "/home/codex/.codex"}[spec.provider]
    try:
        existing = client.containers.get(spec.name) if os.environ.get("CODEX_NODE_BIND_IP") else None
    except NotFound:
        existing = None
    if existing:
        if (existing.labels.get(MANAGED_LABEL) != "true"
                or existing.labels.get("io.codex-gateway.provider", "codex") != spec.provider):
            raise HTTPException(409, "Worker name belongs to another container")
        container = existing
        if existing.status != "running":
            existing.start()
    else:
        try:
            container = client.containers.run(
                image, name=spec.name, detach=True,
                environment={"CODEX_WORKER_TOKEN": os.environ["CODEX_WORKER_TOKEN"]},
                labels={MANAGED_LABEL: "true", "io.codex-gateway.worker": spec.name, "io.codex-gateway.provider": spec.provider},
                network=os.environ["CODEX_DOCKER_NETWORK"], read_only=True,
                ports={"4500/tcp": (os.environ["CODEX_NODE_BIND_IP"], None)} if os.environ.get("CODEX_NODE_BIND_IP") else None,
                volumes={f"{spec.name}-{spec.provider}-home": {"bind": home, "mode": "rw"}, f"{spec.name}-workspaces": {"bind": "/workspace", "mode": "rw"}},
                tmpfs={"/tmp": "size=256m,nosuid,nodev", "/run/codex": "size=1m,noexec,nosuid,nodev,uid=10001,gid=10001"},
                cap_drop=["ALL"], security_opt=["no-new-privileges:true"], pids_limit=256,
                mem_limit="4g", nano_cpus=2_000_000_000, restart_policy={"Name": "unless-stopped"},
            )
        except APIError as exc:
            raise HTTPException(409, "could not create worker") from exc
    host, port = spec.name, "4500"
    if os.environ.get("CODEX_NODE_BIND_IP"):
        container.reload()
        binding = container.attrs["NetworkSettings"]["Ports"]["4500/tcp"][0]
        host, port = os.environ["CODEX_NODE_BIND_IP"], binding["HostPort"]
    return {"id": container.id, "name": spec.name, "endpoint": f"{'http' if spec.provider in {'gemini', 'claude'} else 'ws'}://{host}:{port}"}


@app.put("/workers/{name}/workspaces/{workspace_id}")
def prepare_workspace(name: str, workspace_id: str, authorization: str | None = Header(default=None)) -> dict[str, str]:
    authorize(authorization)
    if not ID_RE.fullmatch(workspace_id):
        raise HTTPException(400, "invalid workspace id")
    container = managed_container(name)
    paths = [f"/workspace/{workspace_id}", *(f"/workspace/{workspace_id}/ws-{slot}" for slot in range(WORKSPACE_SLOT_COUNT))]
    result = container.exec_run(["mkdir", "-p", *paths], user="10001:10001")
    if result.exit_code != 0:
        raise HTTPException(502, "could not prepare workspace")
    return {"path": f"/workspace/{workspace_id}"}


@app.delete("/workers/{name}", status_code=204)
def delete_worker(name: str, authorization: str | None = Header(default=None),
                  expected_id: str | None = None, stopped_before: datetime | None = None) -> None:
    authorize(authorization)
    container = managed_container(name)
    if expected_id and container.id != expected_id:
        raise HTTPException(409, "worker container changed")
    if stopped_before is not None:
        from .retention_policy import RETENTION, stopped_since
        container.reload()
        stopped = stopped_since(container.attrs)
        cutoff = min(stopped_before.astimezone(timezone.utc), datetime.now(timezone.utc) - RETENTION)
        if stopped is None or stopped >= cutoff:
            raise HTTPException(409, "worker is not stopped for more than seven days")
    try:
        # Non-forced removal protects a stopped worker restarted after inspection.
        container.remove(force=stopped_before is None)
    except APIError as exc:
        raise HTTPException(409, "could not remove worker") from exc


@app.get("/workers")
def list_workers(authorization: str | None = Header(default=None)):
    authorize(authorization)
    from .retention_policy import stopped_since
    rows = []
    for container in client.containers.list(all=True, filters={"label": f"{MANAGED_LABEL}=true"}):
        try:
            container.reload()
        except NotFound:
            continue
        stopped = stopped_since(container.attrs)
        rows.append({"id": container.id, "name": container.name,
                     "state": container.status, "stopped_since": stopped.isoformat() if stopped else None})
    return rows


@app.get("/infra")
def infrastructure(authorization: str | None = Header(default=None)):
    authorize(authorization)
    from .docker_monitor import snapshot
    return snapshot(client)
