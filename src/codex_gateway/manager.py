import os
import re

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
    provider: Literal["codex", "gemini"] = "codex"


def authorize(authorization: str | None) -> None:
    expected = os.environ["CODEX_MANAGER_TOKEN"]
    if authorization != f"Bearer {expected}":
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
    try:
        container = client.containers.run(
            os.environ.get("GEMINI_WORKER_IMAGE", "codex-antigravity-worker:1.2.12") if spec.provider == "gemini" else os.environ["CODEX_WORKER_IMAGE"], name=spec.name, detach=True,
            environment={"CODEX_WORKER_TOKEN": os.environ["CODEX_WORKER_TOKEN"]},
            labels={MANAGED_LABEL: "true", "io.codex-gateway.worker": spec.name, "io.codex-gateway.provider": spec.provider},
            network=os.environ["CODEX_DOCKER_NETWORK"], read_only=True,
            volumes={(f"{spec.name}-gemini-home" if spec.provider == "gemini" else f"{spec.name}-codex-home"): {"bind": "/home/agy" if spec.provider == "gemini" else "/home/codex/.codex", "mode": "rw"}, f"{spec.name}-workspaces": {"bind": "/workspace", "mode": "rw"}},
            tmpfs={"/tmp": "size=256m,nosuid,nodev", "/run/codex": "size=1m,noexec,nosuid,nodev,uid=10001,gid=10001"},
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"], pids_limit=256,
            mem_limit="4g", nano_cpus=2_000_000_000, restart_policy={"Name": "unless-stopped"},
        )
    except APIError as exc:
        raise HTTPException(409, "could not create worker") from exc
    return {"id": container.id, "name": spec.name, "endpoint": f"{'http' if spec.provider == 'gemini' else 'ws'}://{spec.name}:4500"}


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
def delete_worker(name: str, authorization: str | None = Header(default=None)) -> None:
    authorize(authorization)
    container = managed_container(name)
    try:
        container.remove(force=True)
    except APIError as exc:
        raise HTTPException(409, "could not remove worker") from exc
