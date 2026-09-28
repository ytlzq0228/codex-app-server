"""Conservative execution continuation using the same scoped identity as history.

Only a verified append to a completed checkpoint resumes a Thread. The DB lease
covers HTTP execution (including streaming), not a long-lived DB connection.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from .client_tools import definitions, ToolProtocolError
from .config import get_settings
from .conversations import digest, explicit_identity, durable_write
from .database import SessionLocal
from .models import ExecutionSession, ResponseBinding, Worker, WorkerStatus

LEASE_SECONDS = 90


def now():
    return datetime.now(timezone.utc)


def normal_item(item):
    """Ignore transport IDs/annotations, preserve text, images and tool identity."""
    from .multimodal import content_parts
    def semantic_content(content):
        parts = content_parts(content)
        if any(p['type'] == 'input_image' for p in parts):
            return parts
        return '\n'.join(p['text'] for p in parts)
    if not isinstance(item, dict):
        return None
    kind = item.get("type", "message")
    if kind == "additional_tools":
        return None
    if kind == "message" and item.get("role") in {"user", "assistant", "developer", "system"}:
        return {"role": item["role"], "text": semantic_content(item.get("content"))}
    if kind in {"function_call", "custom_tool_call"}:
        return {"type": kind, "call_id": item.get("call_id"), "name": item.get("name"),
                "namespace": item.get("namespace"), "input": item.get("arguments", item.get("input"))}
    if kind in {"function_call_output", "custom_tool_call_output"}:
        output = item.get("output")
        return {"type": kind, "call_id": item.get("call_id"),
                "output": semantic_content(output)}
    return None


def history_items(request):
    if not isinstance(request.input, list):
        return None
    items = [i for i in request.input if not (isinstance(i, dict) and i.get("type") == "additional_tools")]
    if len(items) > 10000 or any(normal_item(i) is None for i in items):
        return None
    return items


def hashes(items):
    return [digest(normal_item(i)) for i in items] if items is not None else None


def configuration(request):
    return digest({"version": 1, "model": request.model, "instructions": request.instructions,
                   "tools": definitions(request), "tool_choice": request.tool_choice or "auto",
                   "output_schema": request.output_schema()})


def appended_items(request, expected):
    items = history_items(request)
    actual = hashes(items)
    if not expected or actual is None or actual[:len(expected)] != expected:
        return None
    delta = items[len(expected):]
    # New user turns only. Replays and edited/compacted histories start separately.
    if not delta or not any(i.get("role") == "user" for i in delta):
        return None
    if any(i.get("role") not in {"user", "system", "developer"} for i in delta):
        return None
    return delta


def conflict(message, code="conversation_busy"):
    return HTTPException(409, detail={"error": {"message": message, "type": "invalid_request_error",
                          "code": code, "param": None}}, headers={"Retry-After": "2"})


async def heartbeat(claim):
    try:
        while True:
            await asyncio.sleep(20)
            async with SessionLocal() as db:
                result = await db.execute(update(ExecutionSession).where(
                    ExecutionSession.logical_id == claim["logical_id"],
                    ExecutionSession.lease_token == claim["token"],
                ).values(lease_until=now() + timedelta(seconds=LEASE_SECONDS)))
                await db.commit()
                if not result.rowcount:
                    return
    except asyncio.CancelledError:
        pass


async def prepare(request, principal, endpoint, audit, *, pending_thread=None, binding=None, tool_sessions=None):
    """Claim identity and return an optional active binding; public previous wins."""
    if not audit or not principal.key_id or not get_settings().execution_resume_enabled:
        return request, binding
    from .providers import provider_for
    provider = provider_for(request.model)
    from .audit import request_params
    from .request_observation import request_observation
    observation = request_observation(audit)
    if observation.get("body_capture_truncated") or not observation.get("body_complete") or observation.get("capture_notes"):
        audit["execution_decision"] = {"action": "untracked", "reason": "incomplete_identity_capture"}
        return request, binding
    logical, evidence = explicit_identity(request_params(audit), observation,
                                          principal.key_id, endpoint, "")
    if evidence.get("category") == "thread_title":
        logical = None
    # Tool continuations sometimes omit client metadata. Recover only from a
    # call_id already authenticated by ToolSessions, never from user input alone.
    async with SessionLocal() as db:
        if pending_thread:
            pending = await db.scalar(select(ExecutionSession).where(
                ExecutionSession.api_key_id == principal.key_id, ExecutionSession.endpoint == endpoint,
                ExecutionSession.thread_id == pending_thread,
            ))
            if pending:
                if logical and logical != pending.logical_id:
                    raise conflict("Tool output belongs to a different client conversation", "tool_conversation_mismatch")
                logical = pending.logical_id
        if not logical:
            audit["execution_decision"] = {"action": "untracked", "reason": evidence.get("method", "no_identity")}
            return request, binding
        await db.execute(insert(ExecutionSession).values(logical_id=logical, api_key_id=principal.key_id,
                          endpoint=endpoint, provider=provider, state="new").on_conflict_do_nothing(index_elements=["logical_id"]))
        row = await db.scalar(select(ExecutionSession).where(ExecutionSession.logical_id == logical).with_for_update())
        if (row.provider or "codex") != provider:
            raise conflict("Continuation cannot change provider", "provider_mismatch")
        instant = now()
        if row.lease_token and row.lease_until and row.lease_until > instant:
            raise conflict("Another request is executing in this conversation; retry after it completes")
        if row.state == "invalid" and tool_sessions and row.thread_id:
            await tool_sessions.cancel_thread(principal.key_id, row.thread_id)
        if pending_thread and row.state == "invalid":
            raise ToolProtocolError("This tool binding was invalidated; start a new user turn with full history", "tool_binding_invalidated")
        if pending_thread and row.thread_id and row.thread_id != pending_thread:
            raise conflict("Tool output belongs to a superseded execution", "tool_conversation_mismatch")
        orphaned = False
        superseded = False
        if row.state == "waiting_tool" and not pending_thread:
            live = tool_sessions.has_pending(principal.key_id, row.thread_id) if tool_sessions else False
            # Only a full history followed by a new user turn can rebuild. Never
            # reinterpret an orphaned tool result as permission to rerun tools.
            items = history_items(request)
            full_prefix = (hashes(items) or [])[:len(row.history_hashes or [])]
            full_history = full_prefix == row.history_hashes if row.history_hashes else bool(items and len(items)>1)
            can_supersede = getattr(tool_sessions, "can_supersede_with_user_turn", None) if tool_sessions else None
            superseded = bool(live and full_history and items and items[-1].get("role") == "user"
                              and can_supersede and can_supersede(
                                  request, principal.key_id, row.thread_id, len(row.history_hashes or [])))
            if live and row.expires_at and row.expires_at > instant and not superseded:
                raise conflict("This conversation is waiting for a client tool result; return its call_id first", "conversation_waiting_tool")
            if not items or items[-1].get("role") != "user" or not full_history:
                raise conflict("The pending tool call was lost; send full history with a new user message", "conversation_history_required")
            if live:
                await tool_sessions.cancel_thread(principal.key_id, row.thread_id)
            orphaned = True
        expected = row.history_hashes
        items = history_items(request)
        checkpoint = hashes(items)
        reason, action = "no_checkpoint", "new_thread"
        chosen = binding
        if pending_thread:
            action, reason = "tool_continuation", "authenticated_call_id"
            if expected and checkpoint and checkpoint[:len(expected)] == expected:
                pass
            elif expected and items and all(i.get("type") in {"function_call_output", "custom_tool_call_output"} for i in items):
                checkpoint = expected + checkpoint
            else:
                checkpoint = None
        elif binding:
            action, reason = "explicit_resume", "previous_response_id"
            # The public protocol accepts delta-only input. Preserve that
            # behavior, but never mistake it for the Thread's full history.
            checkpoint = None
        elif row.state != "new":
            if orphaned:
                reason = "pending_tool_superseded" if superseded else "pending_tool_lost"
            elif row.lease_token or row.state != "ready":
                reason = row.invalid_reason or "previous_execution_incomplete"
            elif row.config_hash != configuration(request):
                reason = "configuration_changed"
            else:
                delta = appended_items(request, expected)
                active = await db.scalar(select(ResponseBinding).where(
                    ResponseBinding.response_id == row.response_id, ResponseBinding.api_key_id == principal.key_id,
                    ResponseBinding.status == "active",
                ))
                worker = await db.get(Worker, row.worker_id) if row.worker_id else None
                if delta is None:
                    reason = "history_not_append_only"
                elif not active:
                    reason = "binding_invalidated"
                elif worker and active.worker_generation != worker.execution_generation:
                    reason = "worker_account_changed"
                elif not worker or not worker.enabled or worker.status not in {WorkerStatus.ready, WorkerStatus.busy}:
                    reason = "worker_unavailable"
                elif principal.pinned_worker_id and principal.pinned_worker_id != worker.id:
                    reason = "pinned_worker_changed"
                else:
                    chosen = active
                    request = request.model_copy(update={"previous_response_id": row.thread_id})
                    request._execution_input_text = "\n\n".join(request._item_text(i) for i in delta)
                    request._execution_input_items = delta
                    request._execution_auto_resume = True
                    action, reason = "resume", "explicit_identity_and_history_prefix"
        if provider != "codex" and row.state != "new" and action == "new_thread":
            raise conflict("Gemini conversation cannot be safely resumed; start a new conversation", "conversation_resume_unavailable")
        token = str(uuid4())
        claim = {"logical_id": logical, "token": token, "history": checkpoint,
                 "config": (row.config_hash if pending_thread and row.config_hash else configuration(request)),
                 "action": action, "reason": reason, "previous_thread_id": row.thread_id}
        row.lease_token, row.lease_until, row.state = token, instant + timedelta(seconds=LEASE_SECONDS), "running"
        await db.commit()
    audit["execution"] = claim
    audit["execution_decision"] = {k: claim[k] for k in ("action", "reason", "previous_thread_id")}
    claim["heartbeat"] = asyncio.create_task(heartbeat(claim), name="execution-lease")
    return request, chosen


async def finish(db, audit, result, target, response_id):
    """Persist checkpoint in the same transaction as usage and response binding."""
    claim = (audit or {}).get("execution")
    if not claim:
        return True
    row = await db.scalar(select(ExecutionSession).where(
        ExecutionSession.logical_id == claim["logical_id"],
        ExecutionSession.lease_token == claim["token"],
    ).with_for_update())
    if not row:
        return False  # Stale owner cannot recreate bindings after invalidation.
    expected = claim["history"]
    if result and expected is not None:
        expected = list(expected)
        if result.text:
            expected.append(digest({"role": "assistant", "text": result.text}))
        expected.extend(digest(normal_item(c)) for c in result.tool_calls)
    instant = now()
    row.history_hashes = expected if result else None
    row.config_hash = claim["config"]
    row.state = ("waiting_tool" if result.tool_calls else "ready") if result else "invalid"
    row.thread_id = result.thread_id if result else None
    row.worker_id = target.worker_id
    row.provider = target.provider
    row.response_id = response_id
    row.expires_at = instant + timedelta(seconds=300) if result and result.tool_calls else None
    row.invalid_reason = None if result else "execution_failed"
    row.lease_token = row.lease_until = None
    return True


@durable_write
async def cleanup(audit):
    claim = (audit or {}).get("execution")
    if not claim:
        return
    task = claim.get("heartbeat")
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    # Also covers cancellation, validation/selection failures and failed DB saves.
    async with SessionLocal() as db:
        await db.execute(update(ExecutionSession).where(
            ExecutionSession.logical_id == claim["logical_id"], ExecutionSession.lease_token == claim["token"],
        ).values(lease_token=None, lease_until=None, state="invalid", history_hashes=None))
        await db.commit()
