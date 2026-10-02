"""Claude Code transport, kept separate from the existing Codex backend."""
import json
import logging
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
import httpx
from jsonschema.exceptions import ValidationError
from .backend import WorkerFailure, classify_worker_failure
from .schemas import BackendResult, BackendStreamEvent
from .client_tools import definitions, dynamic_specs, public_call, tool_outputs, ToolProtocolError
from .grammar_tools import call_matches_grammar
from .tool_sessions import ToolSessions

from .gemini_backend import worker_rpc

class ClaudeAdapter:
    def __init__(self, settings):
        self.settings = settings
        self.tool_sessions = ToolSessions(self._turn_events)

    async def stream(self, request, target):
        try:
            events = (self.tool_sessions.stream(request, target) if definitions(request) or tool_outputs(request)
                      else self._turn_events(request, target))
            async with aclosing(events):
                async for event in events:
                    yield event
        except ToolProtocolError as exc:
            raise WorkerFailure(str(exc), kind="request") from exc

    async def _turn_events(self, request, target, tool_run=None):
        from .providers import validate_capabilities, provider_for
        validate_capabilities(request)
        if target.provider != provider_for(request.model):
            raise WorkerFailure("Provider mismatch", kind="request")
        if target.worker_id:
            from .database import SessionLocal
            from .models import Worker
            async with SessionLocal() as db:
                worker = await db.get(Worker, target.worker_id)
                if not worker or worker.provider != "claude" or worker.execution_generation != target.worker_generation or worker.endpoint != target.endpoint:
                    raise WorkerFailure("Worker identity changed", kind="account_changed")
        from uuid import uuid4
        from .claude_images import content_blocks
        payload = {"model": self.settings.model_alias_map().get(request.model, request.model),
                   "session_id": request.previous_response_id or str(uuid4()),
                   "conversation": request.previous_response_id, "workspace": target.workspace,
                   "system": request.instructions, "effort": (request.reasoning or {}).get("effort"),
                   "json_schema": request.output_schema()}
        # The system prompt is sent separately, never duplicated in user content.
        content_request = request.model_copy(update={"instructions": None})
        payload["content"] = await content_blocks(content_request.worker_input())
        specs = definitions(request) if request.tool_choice != "none" else []
        # Keep original names where unambiguous; aliases remain a safe fallback
        # for colliding or overlong namespace encodings.
        from .client_tools import NAME
        names = [(s["namespace"] + "__" if s["namespace"] else "") + s["name"] for s in specs]
        if len(set(names)) == len(names) and all(NAME.fullmatch(n) for n in names):
            specs = [{**s, "alias": n} for s, n in zip(specs, names)]
        validators = {}
        from jsonschema.validators import validator_for
        from jsonschema.exceptions import SchemaError
        for spec in specs:
            cls = validator_for(spec["schema"])
            try:
                cls.check_schema(spec["schema"])
            except SchemaError as exc:
                raise ToolProtocolError("Invalid client function JSON Schema") from exc
            validators[spec["alias"]] = cls(spec["schema"])
        payload["tools"] = [{k: v for k, v in tool.items() if k != "type"} for tool in dynamic_specs(specs)]
        if not specs:
            payload["system"] = (payload["system"] or "") + "\nNo tools are available in this session; answer directly and never write tool invocations."
        output_schema = request.output_schema()
        output_chunks = []
        thread = ""
        grammar_failures = 0
        grammar_needs_correction = False
        # A connection failure is deliberately not automatically retried: the
        # remote CLI may already have accepted the prompt.
        try:
            async with httpx.AsyncClient(timeout=self.settings.app_server_timeout_seconds) as client:
                async with client.stream("POST", target.endpoint + "/turn", json=payload,
                        headers={"Authorization": "Bearer " + self.settings.app_server_token.get_secret_value()}) as response:
                    if response.status_code != 200:
                        raise WorkerFailure("Claude worker rejected execution", kind={409: "capacity", 400: "request", 422: "request", 401: "logged_out", 429: "limit"}.get(response.status_code, "connection"))
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        data = json.loads(line)
                        if data.get("heartbeat"):
                            continue
                        if data.get("error"):
                            raise WorkerFailure(data["error"], kind=data.get("kind", "connection"))
                        thread = data.get("thread_id") or thread
                        if data.get("event") == "client_tool":
                            if tool_run is None or not thread:
                                raise ToolProtocolError("Unexpected Claude client tool request")
                            reply = {"run_id": data["run_id"], "worker_call_id": data["worker_call_id"]}
                            try:
                                call = public_call(specs, data)
                            except ToolProtocolError:
                                if data.get("namespace") or not any(spec["alias"] == data.get("tool") for spec in specs):
                                    raise
                                call = None
                            schema_error = next(validators[data["tool"]].iter_errors(data.get("arguments")), None)
                            if call is None or schema_error is not None or not await call_matches_grammar(specs, data, call):
                                grammar_failures += 1
                                logging.getLogger(__name__).warning(
                                    "Claude client tool correction: alias=%s attempt=%s validator=%s path=%s",
                                    data["tool"], grammar_failures,
                                    schema_error.validator if schema_error else "custom_grammar",
                                    list(schema_error.absolute_path) if schema_error else [],
                                )
                                grammar_needs_correction = True
                                if grammar_failures > 2:
                                    raise ToolProtocolError("Model tool input did not match the declared schema or grammar after two corrections")
                                await worker_rpc(target.endpoint, self.settings, "/tool-result", {
                                    **reply, "is_error": True, "content": [{"type": "text", "text":
                                    "Tool was NOT executed. Match the tool's declared JSON parameters and grammar. "
                                    + ("Schema validation failed at " + "/".join(map(str, schema_error.absolute_path))
                                       + " (" + str(schema_error.validator) + "). " if schema_error else "")
                                    + "Declared schema: " + json.dumps(next(s["schema"] for s in specs if s["alias"] == data["tool"])) + ". "
                                    +
                                    "For a custom tool, send exactly one field: input, containing the raw input string. "
                                    "Do not add Markdown fences or extra fields. Call the tool again with corrected arguments."}]})
                                continue
                            grammar_needs_correction = False
                            await self.tool_sessions.await_result(tool_run, call)
                            yield BackendStreamEvent(thread_id=thread, tool_call=call,
                                input_tokens=data.get("input_tokens", 0), output_tokens=data.get("output_tokens", 0),
                                cache_read_tokens=data.get("cache_read_tokens", 0),
                                cache_write_tokens=data.get("cache_write_tokens", 0))
                            output = await self.tool_sessions.receive_result(tool_run)
                            from .multimodal import user_parts
                            blocks = await content_blocks(user_parts(output))
                            content = [{"type": "text", "text": p["text"]} if p["type"] == "text" else
                                       {"type": "image", "data": p["source"]["data"], "mimeType": p["source"]["media_type"]}
                                       for p in blocks]
                            await worker_rpc(target.endpoint, self.settings, "/tool-result", {**reply, "content": content,
                                             "is_error": tool_run.result_is_error})
                            continue
                        if data.get("done") and grammar_needs_correction:
                            raise ToolProtocolError("Model ended without correcting the invalid client tool input")
                        delta = data.get("delta", "")
                        if output_schema is not None:
                            output_chunks.append(delta)
                            if not data.get("done"):
                                continue
                            from jsonschema.validators import validator_for
                            try:
                                output = "".join(output_chunks).strip()
                                if output.startswith("```") and output.endswith("```"):
                                    output = output.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                                parsed = json.loads(output)
                                validator_for(output_schema)(output_schema).validate(parsed)
                            except (ValueError, IndexError, ValidationError) as exc:
                                raise ToolProtocolError("Claude output did not match the requested JSON Schema",
                                                        "structured_output_invalid") from exc
                            delta = json.dumps(parsed, ensure_ascii=False)
                        yield BackendStreamEvent(thread_id=thread, delta=delta, done=data.get("done", False),
                            input_tokens=data.get("input_tokens", 0), output_tokens=data.get("output_tokens", 0),
                            cache_read_tokens=data.get("cache_read_tokens", 0),
                            cache_write_tokens=data.get("cache_write_tokens", 0))
                        if data.get("done"):
                            return
            raise WorkerFailure("Claude stream ended without a final result")
        except httpx.HTTPError as exc:
            raise WorkerFailure("Claude worker connection failed") from exc
        except (ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, ToolProtocolError):
                raise
            raise WorkerFailure("Claude worker returned an invalid event") from exc

    async def complete(self, request, target):
        text = ""
        calls = []
        terminal = BackendStreamEvent()
        async for event in self.stream(request, target):
            terminal = event
            text += event.delta or ""
            if event.tool_call:
                calls.append(event.tool_call)
        if not terminal.done and not calls:
            raise WorkerFailure("Claude returned no final result")
        return BackendResult(text=text, tool_calls=calls, thread_id=terminal.thread_id,
                             input_tokens=terminal.input_tokens, output_tokens=terminal.output_tokens,
                             cache_read_tokens=terminal.cache_read_tokens, cache_write_tokens=terminal.cache_write_tokens)

async def probe_claude(worker, db, settings, *, inference=True, login_session=None):
    from .contributions import update_account
    from .models import WorkerStatus
    from .quota import reconcile_worker
    try:
        payload = await worker_rpc(worker.endpoint, settings, "/login/verify" if login_session else ("/probe" if inference else "/account"),
                                   {"session_id": login_session} if login_session else None)
        account = payload.get("account")
        if not account:
            raise WorkerFailure("Claude account or inference unavailable", kind=payload.get("kind", "logged_out"))
        await update_account(worker, account)
        if payload.get("available") is False:
            raise WorkerFailure("Claude inference unavailable", kind=payload.get("kind", "connection"))
        worker.status = WorkerStatus.ready
        worker.failure_kind = worker.failure_reason = worker.retry_after = worker.quarantined_at = None
        worker.last_seen_at = datetime.now(timezone.utc)
        ok, message = True, "Claude 账号和模型访问检查通过"
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            return {"ok": False, "busy": True, "logged_in": bool(worker.auth_mode), "message": "Claude 正在执行或登录，请稍后探测"}
        worker.status = WorkerStatus.error
        worker.failure_kind = "connection"
        worker.retry_after = datetime.now(timezone.utc) + timedelta(seconds=settings.worker_failure_cooldown_seconds)
        ok, message = False, "Claude 服务暂时不可用"
    except Exception as exc:
        kind = exc.kind if isinstance(exc, WorkerFailure) else classify_worker_failure(str(exc))
        worker.status = WorkerStatus.error
        worker.failure_kind = kind
        if kind == "logged_out":
            await update_account(worker, None)
        worker.failure_reason = ("账号已登录，但未通过 Claude 资格检查，请更换账号或联系管理员。"
                                 if kind == "ineligible" else "Claude account check failed")
        worker.retry_after = datetime.now(timezone.utc) + timedelta(seconds=settings.worker_limit_cooldown_seconds if kind == "limit" else settings.worker_failure_cooldown_seconds)
        ok, message = False, (worker.failure_reason if kind == "ineligible" else "Claude 检查失败，请检查登录状态或稍后重试")
    await reconcile_worker(db, worker)
    await db.commit()
    return {"ok": ok, "logged_in": bool(worker.auth_mode), "message": message}
