"""Conservative execution continuation using the same scoped identity as history.

Only a verified append to a completed checkpoint resumes a Thread. The DB lease
covers HTTP execution (including streaming), not a long-lived DB connection.
"""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from .client_tools import definitions, tool_outputs, ToolProtocolError
from .config import get_settings
from .conversations import digest, explicit_identity, durable_write
from .database import SessionLocal
from .models import ExecutionSession, ResponseBinding, Worker, WorkerStatus

LEASE_SECONDS = 90


def now():
    return datetime.now(timezone.utc)


def normal_item(item, *, legacy=False):
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
        arguments = item.get("arguments", item.get("input"))
        if kind == "function_call" and not legacy:
            arguments = canonical_arguments(arguments)
        return {"type": kind, "call_id": item.get("call_id"), "name": item.get("name"),
                "namespace": item.get("namespace"), "input": arguments}
    if kind in {"function_call_output", "custom_tool_call_output"}:
        output = item.get("output")
        return {"type": kind, "call_id": item.get("call_id"),
                "output": semantic_content(output), **({"is_error": True} if item.get("is_error") is True else {})}
    return None



def parsed_arguments(value):
    # Duplicate keys/non-finite numbers are ambiguous; preserve their exact text.
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("non-finite JSON number")
    if not isinstance(value, str):
        raise ValueError("arguments must be a JSON string")
    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


def canonical_arguments(value):
    try:
        return json.dumps(parsed_arguments(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return value


def checkpoint_matches(items, expected):
    """Verify every saved digest, including bounded legacy JSON representations.

    Legacy digests cannot be inverted. Accept a formatting variant only when
    its complete old-format item hashes to the saved digest; this proves the
    original tool name, call ID and argument values without trusting new input.
    Unknown legacy encodings fail closed.
    """
    if not expected or items is None or len(items) < len(expected):
        return False
    for item, saved in zip(items, expected):
        if digest(normal_item(item)) == saved:
            continue
        if item.get("type") != "function_call":
            return False
        original = normal_item(item, legacy=True)
        if digest(original) == saved:
            continue
        try:
            arguments = parsed_arguments(original["input"])
            variants = (
                json.dumps(arguments, sort_keys=sort, separators=separators,
                           ensure_ascii=ascii, allow_nan=False)
                for sort in (False, True) for ascii in (False, True)
                for separators in ((",", ":"), (", ", ": "))
            )
            if not any(digest({**original, "input": value}) == saved for value in variants):
                return False
        except (ValueError, TypeError, RecursionError):
            return False
    return True


def recovery_call_id(items, checkpoint_length):
    """A verified checkpoint plus exactly its tool result and a new user turn."""
    if not items or not checkpoint_length or len(items) <= checkpoint_length or items[-1].get("role") != "user":
        return None
    pending = items[checkpoint_length - 1]
    if pending.get("type") not in {"function_call", "custom_tool_call"}:
        return None
    tail = items[checkpoint_length:]
    outputs = [i for i in tail if i.get("type") in {"function_call_output", "custom_tool_call_output"}]
    if len(outputs) != 1 or outputs[0].get("call_id") != pending.get("call_id"):
        return None
    expected_type = "function_call_output" if pending["type"] == "function_call" else "custom_tool_call_output"
    if outputs[0].get("type") != expected_type or tail[0] is not outputs[0]:
        return None
    if any(i.get("role") not in {"user", "assistant", "developer", "system"} for i in tail[1:]):
        return None
    return pending.get("call_id")


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
    if not checkpoint_matches(items, expected):
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


# Bounds apply per gateway process; database leases remain the cross-node arbiter.
_waiters = {}


async def prepare(request, principal, endpoint, audit, *, pending_thread=None, binding=None,
                  tool_sessions=None, is_disconnected=None):
    from .providers import provider_for
    settings = get_settings()
    timeout = settings.claude_execution_wait_seconds if provider_for(request.model) == "claude" and binding is None else 0
    started = time.monotonic()
    scope = None
    attempts = 0
    try:
        while True:
            if attempts:
                if is_disconnected and await is_disconnected():
                    raise HTTPException(499, detail="Client disconnected while waiting for conversation")
                # A pending call may have been consumed or cancelled during the wait.
                if pending_thread and tool_sessions:
                    remote = False
                    if settings.node_id:
                        from .models import PendingToolRoute
                        outputs = tool_outputs(request)
                        async with SessionLocal() as db:
                            route = await db.get(PendingToolRoute, (str(principal.key_id), outputs[0][0])) if len(outputs) == 1 else None
                            remote = bool(route and route.node_id != settings.node_id and
                                          route.thread_id == pending_thread and route.expires_at > now())
                    if not remote:
                        run = tool_sessions.find(request, principal.key_id)
                        if not run or run.thread_id != pending_thread:
                            raise conflict("Tool output belongs to a superseded execution", "tool_conversation_mismatch")
            try:
                result = await _prepare(request, principal, endpoint, audit, pending_thread=pending_thread,
                                        binding=binding, tool_sessions=tool_sessions)
                if scope and audit:
                    audit["execution_decision"]["wait_ms"] = int((time.monotonic() - started) * 1000)
                return result
            except HTTPException as exc:
                logical = getattr(exc, "execution_logical_id", None)
                remaining = timeout - (time.monotonic() - started)
                if not logical or remaining <= 0:
                    raise
                if scope is None:
                    scope = (id(asyncio.get_running_loop()), logical)
                    if _waiters.get(scope, 0) >= settings.claude_execution_max_waiters or sum(_waiters.values()) >= 128:
                        scope = None
                        raise
                    _waiters[scope] = _waiters.get(scope, 0) + 1
                # _prepare has exited its DB context: no transaction/row lock spans this wait.
                await asyncio.sleep(min(0.1, remaining))
                attempts += 1
    finally:
        if scope:
            _waiters[scope] -= 1
            if not _waiters[scope]:
                del _waiters[scope]


async def _prepare(request, principal, endpoint, audit, *, pending_thread=None, binding=None, tool_sessions=None):
    """Claim identity and return an optional active binding; public previous wins."""
    if not audit or not principal.key_id:
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
    if evidence.get("auxiliary_kind") and not pending_thread and not binding and not request.previous_response_id and not tool_outputs(request):
        audit["execution_decision"] = {"action": "auxiliary", "reason": evidence["auxiliary_kind"],
                                       "rule": evidence["auxiliary_rule"], "tools_disabled": True}
        return request.model_copy(update={"tools": [], "tool_choice": "none"}), None
    if not get_settings().execution_resume_enabled:
        return request, binding
    if evidence.get("category") in {"thread_title", "claude_auxiliary"}:
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
            error = conflict("Another request is executing in this conversation; retry after it completes")
            error.execution_logical_id = logical
            raise error
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
            if get_settings().node_id and not live:
                from .models import PendingToolRoute
                live = bool(await db.scalar(select(PendingToolRoute.call_id).where(
                    PendingToolRoute.key_id == str(principal.key_id),
                    PendingToolRoute.thread_id == row.thread_id,
                    PendingToolRoute.expires_at > func.now())))
            # Only a full history followed by a new user turn can rebuild. Never
            # reinterpret an orphaned tool result as permission to rerun tools.
            items = history_items(request)
            full_history = checkpoint_matches(items, row.history_hashes)
            call_id = recovery_call_id(items, len(row.history_hashes or [])) if full_history else None
            if live and call_id:
                from .cluster import supersede_tool
                superseded = await supersede_tool(db, row, request, tool_sessions)
                if superseded:
                    live = False
            if live:
                # A remote pending task must be cancelled by its owner even if
                # the execution checkpoint TTL elapsed slightly earlier.
                raise conflict("This conversation is waiting for a client tool result; return its call_id first", "conversation_waiting_tool")
            if not call_id:
                raise conflict("The pending tool call was lost; send full history with its result and a new user message", "conversation_history_required")
            orphaned = True
        expected = row.history_hashes
        items = history_items(request)
        checkpoint = hashes(items)
        reason, action = "no_checkpoint", "new_thread"
        chosen = binding
        if pending_thread:
            action, reason = "tool_continuation", "authenticated_call_id"
            if checkpoint_matches(items, expected):
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
            # Claude can rebuild flattened history on a fresh CLI session, but
            # a tool reply must go through its authenticated suspended run.
            # Gemini retains its existing resume restriction.
            from .claude_helpers import dialogue_tail
            tail = dialogue_tail(items)
            rebuild = (provider == "claude" and tail and tail.get("role") == "user"
                       and not tool_outputs(request))
            if not rebuild:
                raise conflict("Provider conversation cannot be safely resumed; start a new conversation", "conversation_resume_unavailable")
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
