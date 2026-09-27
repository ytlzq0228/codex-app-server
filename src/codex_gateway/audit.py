"""Observe requests without changing request/response bodies or scheduling."""
from contextvars import ContextVar
import hashlib
import json
import logging
import time
from uuid import uuid4

from .config import get_settings
from .database import SessionLocal
from .models import UsageRecord
from .conversations import explicit_identity, durable_write
from sqlalchemy import select
from .request_observation import capture_transport, request_observation

current_audit: ContextVar[dict | None] = ContextVar("gateway_audit", default=None)
logger = logging.getLogger(__name__)


class RequestAuditMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in {"/v1/responses", "/v1/chat/completions"}:
            return await self.app(scope, receive, send)
        audit = {"body": bytearray(), "saved": False, "principal": None, "status": 500, "complete": False}
        audit.update(transport=capture_transport(scope), body_bytes_received=0,
                     body_hash=hashlib.sha256(), body_complete=False)
        token = current_audit.set(audit)
        started = time.monotonic()

        async def observed_receive():
            message = await receive()
            if message["type"] == "http.request":
                data = message.get("body", b"")
                audit["body_bytes_received"] += len(data)
                audit["body_hash"].update(data)
                audit["body_complete"] = not message.get("more_body", False)
                if len(audit["body"]) + len(data) <= get_settings().max_request_bytes:
                    audit["body"].extend(data)
                else:
                    audit["truncated"] = True
            return message

        async def observed_send(message):
            if message["type"] == "http.response.start":
                audit["status"] = message["status"]
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                audit["complete"] = True
            await send(message)

        try:
            await self.app(scope, observed_receive, observed_send)
        finally:
            try:
                if audit.get("persisted_request_id"):
                    await save_delivery(audit)
                principal = audit["principal"]
                if principal and not audit["saved"]:
                    params = request_params(audit)
                    endpoint = "responses" if scope["path"].endswith("responses") else "chat.completions"
                    logical, evidence = explicit_identity(params, request_observation(audit), principal.key_id, endpoint, "")
                    evidence["execution_outcome"] = "unknown"
                    async with SessionLocal() as db:
                        db.add(UsageRecord(request_id=scope.get("state", {}).get("request_id") or "req_"+uuid4().hex,
                                           api_key_id=principal.key_id, owner_username=principal.owner_username,
                                           model=str(params.get("model", "unknown"))[:120], request_params=params,
                                           logical_conversation_id=logical, conversation_evidence=evidence,
                                           request_observation=request_observation(audit),
                                           status_code=audit["status"] if audit["complete"] else 499,
                                           duration_ms=int((time.monotonic()-started)*1000),
                                           error_code="request_rejected" if audit["complete"] else "request_interrupted",
                                           endpoint="responses" if scope["path"].endswith("responses") else "chat.completions"))
                        await db.commit()
            except Exception:
                logger.exception("Could not persist request audit")
            finally:
                current_audit.reset(token)


def request_params(audit):
    if audit.get("truncated"):
        return {"_capture_error": "request exceeds capture limit"}
    try:
        params = json.loads(audit["body"])
        return params if isinstance(params, dict) else {"_request": params}
    except (ValueError, UnicodeDecodeError):
        return {"_capture_error": "invalid JSON"}


@durable_write
async def save_delivery(audit):
    # Model completion and HTTP stream closure are independent outcomes.
    async with SessionLocal() as db:
        record = await db.scalar(select(UsageRecord).where(UsageRecord.request_id == audit["persisted_request_id"]))
        if record:
            record.request_observation = {**(record.request_observation or {}),
                "response_http_status": audit["status"],
                "response_transport_complete": audit["complete"]}
            await db.commit()
