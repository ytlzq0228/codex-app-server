"""Run inside the gateway container to inventory every registered Gemini worker.

Read-only: verifies transport/tool capability, never invokes inference or changes
worker state. Exit nonzero if any active registered worker needs an upgrade.
"""
import asyncio
import json
from sqlalchemy import select
from codex_gateway.config import get_settings
from codex_gateway.database import SessionLocal, engine
from codex_gateway.models import Worker
from codex_gateway.gemini_backend import worker_rpc


async def main():
    failures = 0
    try:
        async with SessionLocal() as db:
            workers = list(await db.scalars(select(Worker).where(
                Worker.provider == "gemini", Worker.endpoint != "removed://worker")))
        for worker in workers:
            row = {"container": worker.container_name, "enabled": worker.enabled}
            try:
                result = await worker_rpc(worker.endpoint, get_settings(), "/capabilities")
                row["client_tools"] = result.get("client_tools")
                row["ok"] = result.get("client_tools") == 1
            except Exception as exc:
                row.update(ok=False, error=type(exc).__name__)
                if getattr(exc, "response", None) is not None:
                    row["http_status"] = exc.response.status_code
            failures += not row["ok"]
            print(json.dumps(row), flush=True)
    finally:
        await engine.dispose()
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
