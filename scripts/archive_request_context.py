#!/usr/bin/env python3
"""Archive only request context. Run in a one-shot container on the archive node."""
import argparse
import asyncio
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from uuid import UUID

CONTEXT_KEYS = frozenset({"input", "messages", "instructions", "system", "contents", "systemInstruction"})
MARKER = "_gateway_context_archive"

def encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()

def projection(params, reference):
    if not isinstance(params, dict):
        return params, []
    if MARKER in params:
        raise ValueError("Reserved archive marker already exists; refusing to change request")
    fields = sorted(CONTEXT_KEYS.intersection(params))
    if not fields:
        return params, []
    return {**{k: v for k, v in params.items() if k not in CONTEXT_KEYS},
            MARKER: {**reference, "fields": fields}}, fields

def save_verified(root, record_id, request_id, raw, created_at):
    """Write original JSON losslessly; durable publication precedes DB changes."""
    checksum = hashlib.sha256(raw.encode()).hexdigest()
    relative = Path(created_at.strftime("%Y/%m/%d")) / (str(UUID(str(record_id))) + "-" + checksum + ".json.gz")
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = encode({"version": 1, "id": str(record_id), "request_id": request_id,
                      "request_params_json": raw, "sha256": checksum})
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(gzip.compress(payload, compresslevel=6, mtime=0))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    result = read_verified(root, str(relative), checksum, str(record_id))
    if result != raw:
        raise ValueError("Archive read-back differs from original")
    # Sync every new directory entry up to the mounted root.
    current = path.parent
    while True:
        descriptor = os.open(current, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if current == root:
            break
        current = current.parent
    return {"path": str(relative), "sha256": checksum, "version": 1}

def read_verified(root, relative, checksum, record_id):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Archive path escapes root")
    with gzip.open(path, "rb") as stream:
        value = json.load(stream)
    raw = value["request_params_json"]
    if value["id"] != record_id or value["sha256"] != checksum or hashlib.sha256(raw.encode()).hexdigest() != checksum:
        raise ValueError("Archive checksum or identity mismatch")
    return raw

DDL = """
CREATE TABLE IF NOT EXISTS request_context_archives (
 usage_id uuid PRIMARY KEY REFERENCES usage_records(id),
 request_id varchar(80) NOT NULL,
 archived_at timestamptz NOT NULL DEFAULT now(),
 path text,
 sha256 varchar(64),
 context_fields json NOT NULL,
 original_bytes bigint NOT NULL,
 retained_bytes bigint NOT NULL
)
"""
CANDIDATE = """
SELECT u.id, u.request_id, u.created_at, u.request_params::text AS raw
FROM usage_records u
WHERE u.created_at < $1 AND u.request_params IS NOT NULL
AND NOT EXISTS (SELECT 1 FROM request_context_archives a WHERE a.usage_id=u.id)
ORDER BY u.created_at,u.id LIMIT 1 FOR UPDATE OF u SKIP LOCKED
"""

def retryable_database_error(error):
    """Inspect exception chains: transaction cleanup can mask a dropped socket."""
    import asyncpg
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (ConnectionError, TimeoutError, asyncpg.PostgresConnectionError)):
            return True
        if getattr(error, "sqlstate", None) in {
            "57P01", "57P02", "57P03", "25006", "40001", "40P01", "55P03",
        }:
            return True
        error = error.__cause__ or error.__context__
    return False


async def run(args):
    # Keep the original cutoff and committed progress across reconnects.
    state = {"cutoff": None, "completed": 0, "archived": 0, "original": 0, "retained": 0}
    for attempt in range(args.max_retries + 1):
        try:
            return await run_attempt(args, state)
        except Exception as error:
            if not retryable_database_error(error) or attempt >= args.max_retries:
                print(json.dumps({"status": "failed", "error_type": type(error).__name__,
                                  "retries": attempt, "processed": state["completed"]}), flush=True)
                raise
            delay = min(args.retry_delay * (2 ** attempt), 60)
            # Never log error text: database errors can contain request data.
            print(json.dumps({"status": "retrying", "error_type": type(error).__name__,
                              "retry": attempt + 1, "delay_seconds": delay,
                              "processed": state["completed"]}), flush=True)
            await asyncio.sleep(delay)


async def run_attempt(args, state):
    import asyncpg
    url = os.environ["CODEX_GATEWAY_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    db = await asyncpg.connect(url, timeout=20, command_timeout=60,
                               server_settings={"application_name": "request-context-archiver"})
    root = args.root.resolve()
    try:
        if args.read:
            row = await db.fetchrow("SELECT * FROM request_context_archives WHERE usage_id::text=$1 OR request_id=$1", args.read)
            if not row or not row["path"]:
                raise ValueError("No archived context for this usage ID")
            print(read_verified(root, row["path"], row["sha256"], str(row["usage_id"])))
            return
        if not await db.fetchval("SELECT pg_try_advisory_lock(719342031)"):
            print('{"status":"skipped","reason":"archive job already running"}')
            return
        if args.initialize:
            await db.execute(DDL)
            print('{"status":"initialized"}')
            return
        if state["cutoff"] is None:
            state["cutoff"] = await db.fetchval("SELECT now() - interval '14 days'")
        cutoff = state["cutoff"]
        if args.dry_run:
            rows = await db.fetchrow("""
SELECT count(*) requests, sum(pg_column_size(request_params)) params_bytes
FROM usage_records u WHERE created_at < $1 AND request_params IS NOT NULL
AND NOT EXISTS (SELECT 1 FROM request_context_archives a WHERE a.usage_id=u.id)
""", cutoff)
            print(json.dumps({"cutoff": str(cutoff), **dict(rows)}))
            return
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        print(json.dumps({"status": "connected", "cutoff": str(cutoff),
                          "processed": state["completed"]}), flush=True)
        while state["completed"] < args.max_records:
            if shutil.disk_usage(root).free < args.min_free_gib * 1024**3:
                raise RuntimeError("Archive disk free space below configured reserve; DB data retained")
            async with db.transaction():
                await db.execute("SET LOCAL lock_timeout='3s'")
                row = await db.fetchrow(CANDIDATE, cutoff)
                if not row:
                    break
                raw = row["raw"]
                params = json.loads(raw)
                projected, fields = projection(params, {})
                reference = {}
                if fields:
                    reference = await asyncio.to_thread(save_verified, root, row["id"], row["request_id"], raw, row["created_at"])
                    projected, fields = projection(params, reference)
                    await db.execute("UPDATE usage_records SET request_params=$1::json WHERE id=$2",
                                     encode(projected).decode(), row["id"])
                kept = len(encode(projected)) if fields else len(raw.encode())
                await db.execute("""
INSERT INTO request_context_archives
(usage_id,request_id,path,sha256,context_fields,original_bytes,retained_bytes)
VALUES($1,$2,$3,$4,$5::json,$6,$7)
""", row["id"], row["request_id"], reference.get("path"), reference.get("sha256"),
                    json.dumps(fields), len(raw.encode()), kept)
            # Advance only after COMMIT. If its acknowledgement is lost, the
            # durable index makes the next scan skip the already committed row.
            state["original"] += len(raw.encode())
            state["retained"] += kept
            state["archived"] += bool(fields)
            state["completed"] += 1
            if state["completed"] % 100 == 0:
                print(json.dumps({"processed": state["completed"],
                                  "archived": state["archived"]}), flush=True)
        print(json.dumps({"status": "completed", "cutoff": str(cutoff),
                          "processed": state["completed"], "archived": state["archived"],
                          "original_json_bytes": state["original"],
                          "retained_json_bytes": state["retained"]}), flush=True)
    except asyncpg.InterfaceError as error:
        # A socket may close while gzip/fsync runs, before the next DB call.
        # In that case asyncpg raises InterfaceError without a connection cause.
        if db.is_closed():
            raise ConnectionError("database connection closed") from error
        raise
    finally:
        # Do not let cleanup hide the original exception or stall reconnection.
        try:
            await db.close(timeout=5)
        except Exception:
            db.terminate()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-retries", type=int, default=8,
                        help="Maximum reconnect retries per invocation (default: 8)")
    parser.add_argument("--retry-delay", type=float, default=5,
                        help="Initial retry delay in seconds; doubles up to 60 seconds")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--read", help="Request ID or usage UUID; print verified original request JSON for an operator")
    parser.add_argument("--max-records", type=int, default=1000000)
    parser.add_argument("--min-free-gib", type=int, default=10)
    args = parser.parse_args()
    if not 0 <= args.max_retries <= 20 or not 0 <= args.retry_delay <= 60:
        parser.error("max-retries must be 0..20 and retry-delay must be 0..60")
    try:
        asyncio.run(run(args))
    except Exception:
        # run() already emitted a safe failure event; avoid SQL/body traceback leaks.
        raise SystemExit(1) from None
