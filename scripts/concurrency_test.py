#!/usr/bin/env python3
"""Run the two requested concurrency scenarios against a remote gateway."""

import argparse
import os
import asyncio
import base64
import json
import statistics
import subprocess
import time
from collections import Counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REMOTE_CREATE = r'''
import asyncio, json
from codex_gateway.database import SessionLocal
from codex_gateway.models import ApiKey
from codex_gateway.security import generate_api_key, hash_api_key
from codex_gateway.config import get_settings

async def main():
    stamp = str(int(__import__('time').time()))
    rows = []
    async with SessionLocal() as session:
        for i in range(20):
            raw, prefix = generate_api_key()
            record = ApiKey(name=f"concurrency-test-{stamp}-{i+1:02d}", prefix=prefix,
                            key_hash=hash_api_key(raw, get_settings().key_pepper.get_secret_value()),
                            scheduling_mode="pooled")
            session.add(record)
            rows.append((record, raw))
        await session.commit()
        print(json.dumps([{"id": str(r.id), "key": k} for r, k in rows]))
asyncio.run(main())
'''

REMOTE_DELETE = r'''
import asyncio, sys
from datetime import datetime, timezone
from uuid import UUID
from sqlalchemy import delete
from codex_gateway.database import SessionLocal
from codex_gateway.models import ApiKey, ResponseBinding
async def main():
    ids = [UUID(x) for x in sys.argv[1:]]
    async with SessionLocal() as session:
        for key_id in ids:
            row = await session.get(ApiKey, key_id)
            if row:
                row.enabled = False
                row.deleted_at = datetime.now(timezone.utc)
                await session.execute(delete(ResponseBinding).where(ResponseBinding.api_key_id == key_id))
        await session.commit()
asyncio.run(main())
'''


def ssh_python(host, container, code, args=()):
    encoded = base64.b64encode(code.encode()).decode()
    quoted_args = " ".join(args)
    remote = (f"printf %s {encoded} | base64 -d | sudo -n docker exec -i {container} "
              f"python -c 'import sys; exec(sys.stdin.read())' {quoted_args}")
    cmd = ["ssh", "-o", "BatchMode=yes", host, remote]
    return subprocess.run(cmd, check=True, text=True, capture_output=True).stdout.strip()


def post(base_url, key, number):
    body = json.dumps({"model": "codex", "input": f"Concurrency probe {number}. Reply only OK.",
                       "max_output_tokens": 8}).encode()
    request = Request(base_url + "/v1/responses", data=body, method="POST",
                      headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=330) as response:
            payload = response.read().decode("utf-8", "replace")
            return response.status, time.perf_counter() - started, payload
    except HTTPError as exc:
        return exc.code, time.perf_counter() - started, exc.read().decode("utf-8", "replace")
    except (URLError, TimeoutError, OSError) as exc:
        return 0, time.perf_counter() - started, repr(exc)


async def scenario(name, base_url, keys):
    gate = asyncio.Event()
    async def one(i, key):
        await gate.wait()
        return await asyncio.to_thread(post, base_url, key, i)
    tasks = [asyncio.create_task(one(i + 1, key)) for i, key in enumerate(keys)]
    started = time.perf_counter()
    gate.set()
    results = await asyncio.gather(*tasks)
    wall = time.perf_counter() - started
    times = sorted(item[1] for item in results)
    errors = Counter()
    for status, _, payload in results:
        if status != 200:
            try:
                data = json.loads(payload)
                errors[(status, data.get("error", {}).get("code", "unknown"))] += 1
            except Exception:
                errors[(status, payload[:100])] += 1
    def pct(p):
        return times[min(len(times) - 1, int((len(times) - 1) * p))]
    report = {"scenario": name, "requests": len(results), "wall_seconds": round(wall, 3),
              "status": dict(Counter(str(r[0]) for r in results)),
              "latency_seconds": {"min": round(times[0], 3), "avg": round(statistics.mean(times), 3),
                                  "p50": round(pct(.50), 3), "p95": round(pct(.95), 3),
                                  "max": round(times[-1], 3)},
              "errors": [{"status": k[0], "code": k[1], "count": v} for k, v in errors.items()]}
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("CODEX_TEST_SSH"), required="CODEX_TEST_SSH" not in os.environ, help="SSH target, e.g. user@test-host (env CODEX_TEST_SSH)")
    parser.add_argument("--base-url", default=os.environ.get("CODEX_TEST_BASE_URL"), required="CODEX_TEST_BASE_URL" not in os.environ, help="Gateway URL (env CODEX_TEST_BASE_URL)")
    parser.add_argument("--container", default="codex-app-server-gateway-1")
    args = parser.parse_args()
    rows = json.loads(ssh_python(args.host, args.container, REMOTE_CREATE))
    print(f"created {len(rows)} temporary keys", flush=True)
    try:
        await scenario("single_key_40", args.base_url, [rows[0]["key"]] * 40)
        await scenario("twenty_keys_2_each", args.base_url, [r["key"] for r in rows for _ in range(2)])
    finally:
        ssh_python(args.host, args.container, REMOTE_DELETE, [r["id"] for r in rows])
        print("deleted temporary keys", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
