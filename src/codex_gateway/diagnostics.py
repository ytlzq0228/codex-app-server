"""Persist safe exception structure, never exception text, arguments or locals."""
import os
import json
import logging
import traceback
from pathlib import PurePath
from .audit import current_audit

def capture_failure(exc):
    audit = current_audit.get()
    if audit is None:
        return
    audit["diagnostic"] = failure_diagnostic(exc)
    if audit.get("remote_diagnostic"):
        audit["diagnostic"]["remote"] = audit["remote_diagnostic"]
    logging.getLogger(__name__).warning("Request failure diagnostic: %s", json.dumps({
        "request_id": audit.get("request_id"),
        "worker_id": str((audit.get("backend_context") or {}).get("worker_id") or ""),
        **audit["diagnostic"]}))


def failure_diagnostic(exc):
    chain, seen, context = [], set(), []
    while exc is not None and id(exc) not in seen and len(chain) < 6:
        seen.add(id(exc))
        if details := getattr(exc, "gateway_diagnostic", None):
            context.append(details)
        chain.append({"exception": type(exc).__name__,
                      "frames": [{"file": PurePath(f.filename).name, "line": f.lineno, "function": f.name}
                                 for f in traceback.extract_tb(exc.__traceback__)[-8:]]})
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    from .config import get_settings
    result = {"exceptions": chain, "node": get_settings().node_id,
              "release": os.environ.get("CODEX_GATEWAY_RELEASE", "unknown")[:120]}
    if context:
        result["context"] = context
    # No SQL or new connection: this must work even when the pool is exhausted.
    from .database import engine
    pool = engine.pool
    if all(hasattr(pool, method) for method in ("size", "checkedout", "overflow")):
        result["database_pool"] = {"size": pool.size(), "checked_out": pool.checkedout(),
                                   "overflow": pool.overflow(),
                                   "max_overflow": get_settings().database_max_overflow}
    return result


def annotate_failure(exc, category, **fields):
    """Internal callers supply only counters, enums and server-owned identifiers."""
    exc.gateway_diagnostic = {"category": category, **fields}
    return exc



def worker_process_context(value):
    """Worker payloads are filtered before becoming durable diagnostics."""
    if not isinstance(value, dict):
        return {}
    result = {name: value[name] for name in
              ("exit_code", "elapsed_ms", "last_event_age_ms", "last_progress_age_ms", "event_count", "stderr_bytes")
              if type(value.get(name)) is int}
    if type(value.get("produced_output")) is bool:
        result["produced_output"] = value["produced_output"]
    if value.get("last_event") in ("none", "other", "heartbeat", "client_tool", "system",
                                   "rate_limit_event", "stream_event", "result", "assistant", "user"):
        result["last_event"] = value["last_event"]
    if isinstance(value.get("stderr_categories"), list):
        result["stderr_categories"] = sorted({v for v in value["stderr_categories"]
            if isinstance(v, str) and v in {"auth", "quota", "network", "timeout",
                                            "memory", "session", "permission"}})
    return result
