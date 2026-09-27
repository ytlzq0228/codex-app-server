"""Bounded transport metadata for studying client conversation identifiers.

Explicit client identity is also used by the guarded execution continuation.
"""
from datetime import datetime, timezone
import json
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit, urlencode
from starlette.requests import Request

REDACTED = "[REDACTED]"
MAX_FIELD = 8192
MAX_TOTAL = 65536
MAX_PAIRS = 128


def sensitive_name(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return any(part in normalized for part in (
        "authorization", "cookie", "apikey", "token", "secret", "password",
        "credential", "signature", "authentication", "xauth",
    )) or normalized in {"key", "auth", "code", "passwd", "pwd"}


def scrub_json(value, depth=0):
    if depth > 12:
        return "[DEPTH LIMIT]"
    if isinstance(value, dict):
        return {key: REDACTED if sensitive_name(key) else scrub_json(item, depth + 1)
                for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_json(item, depth + 1) for item in value]
    if isinstance(value, str):
        return scrub_value("", value, depth + 1)
    return value


def scrub_value(name: str, value: str, depth=0) -> str:
    if sensitive_name(name) or value.lstrip().lower().startswith(("bearer ", "basic ")):
        return REDACTED
    # Drop oversized values entirely: clipping a JSON credential before parsing
    # could leave its secret visible in a partial document.
    if len(value) > MAX_FIELD:
        return "[OMITTED: VALUE TOO LONG]"
    if depth > 12:
        return "[DEPTH LIMIT]"
    if value.lstrip().startswith(("{", "[")):
        try:
            return json.dumps(scrub_json(json.loads(value), depth + 1), ensure_ascii=False)
        except (ValueError, RecursionError):
            return "[OMITTED: INVALID STRUCTURED VALUE]"
    if value.startswith(("http://", "https://")):
        try:
            url = urlsplit(value)
            pairs = parse_qsl(url.query, keep_blank_values=True, max_num_fields=MAX_PAIRS)
            query = urlencode([(key, scrub_value(key, item, depth + 1)) for key, item in pairs])
            return urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, query, ""))
        except ValueError:
            return "[OMITTED: INVALID URL]"
    return value


def capture_transport(scope) -> dict:
    # Uvicorn applies the trusted-proxy policy before application middleware.
    # Never derive this address from raw XFF here: callers can forge that header.
    client = Request(scope).client
    result = {
        "version": 2,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "gateway_request_id": scope.get("state", {}).get("request_id"),
        "method": scope["method"], "path": scope["path"],
        "http_version": scope.get("http_version"), "scheme": scope.get("scheme"),
        "client_address": list(scope["client"]) if scope.get("client") else None,
        "client_ip": client.host if client else None,
        "headers": [], "query_params": [], "capture_notes": [],
    }
    remaining = MAX_TOTAL

    def capture_pairs(target, pairs):
        nonlocal remaining
        for index, (name, value) in enumerate(pairs):
            size = len(name.encode("utf-8")) + len(value.encode("utf-8"))
            if index >= MAX_PAIRS or size > remaining or len(name) > 256:
                result["capture_notes"].append(target + ": capture limit reached")
                break
            remaining -= size
            result[target].append({"name": name, "value": scrub_value(name, value)})

    capture_pairs("headers", ((name.decode("latin-1").lower(), value.decode("latin-1"))
                              for name, value in scope.get("headers", [])))
    query = scope.get("query_string", b"")
    if len(query) > MAX_TOTAL:
        result["capture_notes"].append("query_params: capture limit reached")
    else:
        try:
            capture_pairs("query_params", parse_qsl(query.decode("utf-8", errors="replace"),
                                                   keep_blank_values=True, max_num_fields=MAX_PAIRS))
        except ValueError:
            result["capture_notes"].append("query_params: too many parameters")
    return result


def request_observation(audit) -> dict | None:
    if audit is None:
        return None
    return {**audit["transport"], "body_bytes_received": audit["body_bytes_received"],
            "body_sha256": audit["body_hash"].hexdigest(),
            "body_complete": audit["body_complete"],
            "body_capture_truncated": bool(audit.get("truncated"))}
