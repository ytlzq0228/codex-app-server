"""Idempotent owner cancellation, independent of an ingress HTTP lifetime."""
import asyncio
import hashlib
import time
from fastapi import HTTPException

def failure(code, message, status=503):
    return HTTPException(status, detail={"error": {"code": code, "message": message,
                         "type": "server_error" if status >= 500 else "invalid_request_error", "param": None}})

def operation_id(action, payload):
    # All components are checked against the authenticated execution on the owner.
    parts = [action, payload["key_id"], payload["logical_id"], payload["response_id"]]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()

class Cancellations:
    def __init__(self, limit=2048, retention=600, wait_seconds=1):
        self.entries = {}
        self.limit = limit
        self.retention = retention
        self.wait_seconds = wait_seconds

    async def run(self, operation, factory, *, status_only=False):
        instant = time.monotonic()
        for key, (created, task) in list(self.entries.items()):
            if task.done() and instant - created > self.retention:
                del self.entries[key]
        entry = self.entries.get(operation)
        if entry is None:
            if status_only:
                raise failure("tool_cancellation_unknown", "Cancellation status is unknown; retry the cancellation request", 409)
            if len(self.entries) >= self.limit:
                raise failure("tool_cancellation_capacity", "Too many pending cancellations; retry shortly")
            task = asyncio.create_task(factory(), name="owner-tool-cancellation")
            # Retrieve failures even when the ingress disconnects.
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            entry = self.entries[operation] = (instant, task)
        task = entry[1]
        # asyncio.wait never propagates ingress cancellation into the owner task.
        done, _ = await asyncio.wait({task}, timeout=0 if status_only else self.wait_seconds)
        if not done:
            return {"operation_id": operation, "cancelled": False, "status": "pending"}
        if task.cancelled():
            raise failure("tool_cancellation_unconfirmed", "Owner cancellation was interrupted")
        cancelled = bool(task.result())
        if not cancelled:
            # A result already claimed by another request cannot be cancelled.
            raise failure("tool_cancellation_conflict", "The tool result is already being processed", 409)
        return {"operation_id": operation, "cancelled": True, "status": "completed"}
