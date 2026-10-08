"""Anthropic Messages wire protocol over the authenticated Responses pipeline.

Stage one rebuilds text/tool SSE; thinking signatures are not replayed.
"""
import json
from uuid import uuid4

from .config import get_settings


def text_blocks(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, list) or any(not isinstance(p, dict) or p.get("type") != "text" or not isinstance(p.get("text"), str) for p in value):
        raise ValueError("Only text system blocks are supported")
    return "\n".join(p["text"] for p in value)


def input_block(part):
    if part.get("type") == "text" and isinstance(part.get("text"), str):
        return {"type": "input_text", "text": part["text"]}
    if part.get("type") != "image":
        raise ValueError("Only text and image input blocks are supported")
    source = part["source"]
    if source.get("type") == "base64":
        if source.get("media_type") not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            raise ValueError("Unsupported image media type")
        url = "data:" + source["media_type"] + ";base64," + source["data"]
    elif source.get("type") == "url":
        url = source["url"]
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("Image source requires a public HTTP(S) URL")
    else:
        raise ValueError("Unsupported image source")
    from .multimodal import image_part
    image_part({"type": "input_image", "image_url": url})
    return {"type": "input_image", "image_url": url}


def translate_request(body, model=None, stream=None):
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object")
    if set(body) & {"mcp_servers", "container", "attachments", "files"}:
        raise ValueError("MCP connectors, containers, attachments and files are not supported")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a nonempty array")
    if messages[-1].get("role") == "assistant":
        raise ValueError("Assistant prefill is not supported")
    items = []
    for message in messages:
        role = message["role"]
        if role not in {"user", "assistant", "system"}:
            raise ValueError("Unsupported message role")
        content = message["content"]
        # Claude Code 2.1.288 --bare appends an empty system reminder.
        if role == "system" and content == []:
            continue
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list) or not content:
            raise ValueError("Message content must be text or a nonempty block array")
        parts = []
        def flush():
            if parts:
                items.append({"role": "developer" if role == "system" else role, "content": list(parts)})
                parts.clear()
        for part in content:
            kind = part.get("type")
            if kind in {"thinking", "redacted_thinking"} and role == "assistant":
                continue
            if kind == "tool_use" and role == "assistant":
                flush()
                if not isinstance(part.get("input"), dict):
                    raise ValueError("tool_use.input must be an object")
                items.append({"type": "function_call", "call_id": part["id"], "name": part["name"],
                              "arguments": json.dumps(part["input"], ensure_ascii=False)})
            elif kind == "tool_result" and role == "user":
                flush()
                output = part.get("content", "")
                if isinstance(output, list):
                    output = [input_block(p) for p in output]
                elif not isinstance(output, str):
                    raise ValueError("tool_result.content must be text or a block array")
                items.append({"type": "function_call_output", "call_id": part["tool_use_id"], "output": output,
                              **({"is_error": True} if part.get("is_error") is True else {})})
            else:
                block = input_block(part)
                if role != "user" and block["type"] == "input_image":
                    raise ValueError("Images require user role")
                if role == "assistant":
                    block["type"] = "output_text"
                parts.append(block)
        flush()
    tools = []
    for tool in body.get("tools") or []:
        if tool.get("type", "custom") != "custom":
            raise ValueError("This gateway does not provide server tools")
        tools.append({"type": "function", "name": tool["name"], "description": tool.get("description", ""),
                      "parameters": tool["input_schema"]})
    choice = body.get("tool_choice") or {"type": "auto"}
    if not isinstance(choice, dict) or choice.get("type") not in {"auto", "none"}:
        raise ValueError("Only auto and none tool_choice are supported")
    result = {"model": model or body["model"], "input": items, "tools": tools,
              "tool_choice": choice["type"], "parallel_tool_calls": False,
              "stream": body.get("stream", False) if stream is None else stream}
    if not isinstance(result["stream"], bool):
        raise ValueError("stream must be a boolean")
    if "system" in body:
        result["instructions"] = text_blocks(body["system"])
    config = body.get("output_config") or {}
    if "effort" in config:
        result["reasoning"] = {"effort": config["effort"]}
    if "format" in config:
        fmt = config["format"]
        if fmt.get("type") != "json_schema" or not isinstance(fmt.get("schema"), dict):
            raise ValueError("Only JSON Schema output is supported")
        result["text"] = {"format": {"type": "json_schema", "schema": fmt["schema"]}}
    metadata = body.get("metadata") or {}
    if "user_id" in metadata:
        if not isinstance(metadata["user_id"], str):
            raise ValueError("metadata.user_id must be a string")
        result["metadata"] = {"user_id": metadata["user_id"]}
        result["safety_identifier"] = metadata["user_id"]
    return result


def usage(value):
    details = value.get("input_tokens_details") or {}
    read, write = details.get("cached_tokens", 0), details.get("cache_write_tokens", 0)
    return {"input_tokens": max(0, value.get("input_tokens", 0) - read - write),
            "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
            "output_tokens": value.get("output_tokens", 0)}


def native_response(value, model):
    content = []
    for item in value.get("output", []):
        if item.get("type") == "message":
            content.extend({"type": "text", "text": p["text"]} for p in item.get("content", []) if p.get("type") == "output_text")
        elif item.get("type") == "function_call":
            content.append({"type": "tool_use", "id": item["call_id"], "name": item["name"],
                            "input": json.loads(item["arguments"])})
    return {"id": "msg_" + value["id"], "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": "tool_use" if any(p["type"] == "tool_use" for p in content) else "end_turn",
            "stop_sequence": None, "usage": usage(value.get("usage") or {})}


def native_error(value, status=400):
    error = value.get("error", value.get("detail", value))
    if not isinstance(error, dict):
        error = {"message": str(error)}
    code = error.get("code", "")
    if status in {401, 403}:
        pass
    elif code == "provider_quota_exhausted":
        status = 429
    elif code in {"worker_capacity_exceeded", "worker_queue_timeout"}:
        status = 529
    elif code == "worker_execution_conflict":
        status = 409
    elif code in {"worker_connection_timeout", "worker_transport_timeout"}:
        status = 504
    elif code in {"previous_response_not_found", "model_not_found"}:
        status = 404
    elif code.startswith(("invalid_", "client_tool")) or code in {"unsupported_parameter", "structured_output_invalid"}:
        status = 400
    elif status == 422:
        status = 400
    elif status == 502:
        status = 500
    kind = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
            404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error",
            503: "overloaded_error", 529: "overloaded_error"}.get(status, "api_error")
    return status, {"type": "error", "error": {"type": kind, "message": error.get("message", str(error))}}


class MessageStream:
    def __init__(self, model):
        self.model, self.index = model, 0
        self.started = self.open_text = self.finished = False

    def translate(self, obj):
        kind = obj.get("type")
        if self.finished:
            return []
        if kind == "error" or kind == "response.failed":
            self.finished = True
            error = obj if kind == "error" else obj["response"]
            return [native_error(error, 400 if str(error.get("code", "")).startswith(("invalid_", "client_tool")) else 500)[1]]
        events = []
        if not self.started:
            response = obj.get("response") or {}
            events.append({"type": "message_start", "message": {"id": "msg_" + response.get("id", uuid4().hex),
                "type": "message", "role": "assistant", "model": self.model, "content": [],
                "stop_reason": None, "stop_sequence": None, "usage": usage({})}})
            self.started = True
        if kind == "response.output_text.delta":
            if not self.open_text:
                events.append({"type": "content_block_start", "index": self.index, "content_block": {"type": "text", "text": ""}})
                self.open_text = True
            events.append({"type": "content_block_delta", "index": self.index, "delta": {"type": "text_delta", "text": obj["delta"]}})
        if kind in {"response.output_text.done", "response.completed", "response.output_item.done"} and self.open_text:
            events.append({"type": "content_block_stop", "index": self.index})
            self.index += 1
            self.open_text = False
        if kind == "response.output_item.done" and obj["item"].get("type") == "function_call":
            call = obj["item"]
            events += [{"type": "content_block_start", "index": self.index, "content_block": {
                       "type": "tool_use", "id": call["call_id"], "name": call["name"], "input": {}}},
                       {"type": "content_block_delta", "index": self.index, "delta": {
                       "type": "input_json_delta", "partial_json": call["arguments"]}},
                       {"type": "content_block_stop", "index": self.index}]
            self.index += 1
        if kind == "response.completed":
            response = native_response(obj["response"], self.model)
            events += [{"type": "message_delta", "delta": {"stop_reason": response["stop_reason"],
                       "stop_sequence": None}, "usage": response["usage"]}, {"type": "message_stop"}]
            self.finished = True
        return events


class ClaudeNativeMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope["path"] not in {"/v1/messages", "/v1/messages/count_tokens"}:
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope["headers"]}
        version = headers.get(b"anthropic-version")
        async def fail(message, status=400):
            status, obj = native_error({"message": message}, status)
            request_id = ("req_" + uuid4().hex).encode()
            failure_headers = [(b"content-type", b"application/json"), (b"request-id", request_id),
                               (b"x-request-id", request_id), (b"x-gateway-generation-policy", b"worker-defaults")]
            if version:
                failure_headers.append((b"anthropic-version", version))
            await send({"type": "http.response.start", "status": status, "headers": failure_headers})
            await send({"type": "http.response.body", "body": json.dumps(obj).encode()})
        if not version:
            return await fail("anthropic-version header is required")
        raw = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            raw.extend(event.get("body", b""))
            if len(raw) > get_settings().max_request_bytes:
                return await fail("Request body is too large", 413)
            if not event.get("more_body", False):
                break
        count = scope["path"].endswith("/count_tokens")
        try:
            original = json.loads(raw)
            requested = original["model"]
            settings = get_settings()
            aliases = dict(p.strip().split(":", 1) for p in settings.claude_native_model_aliases.split(",") if p.strip())
            model = aliases.get(requested, requested)
            from .providers import provider_for
            if provider_for(model) != "claude":
                return await fail("Claude model is not available", 404)
            body = translate_request(original, model, False if count else None)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            return await fail(str(exc))
        stream = body["stream"]
        data = json.dumps(body, ensure_ascii=False).encode()
        forwarded = [(k, v) for k, v in scope["headers"] if k.lower() not in {b"content-length", b"x-api-key"}]
        if b"authorization" not in headers and headers.get(b"x-api-key"):
            forwarded.append((b"authorization", b"Bearer " + headers[b"x-api-key"]))
        forwarded.append((b"content-length", str(len(data)).encode()))
        path = "/v1/responses/count_tokens" if count else "/v1/responses"
        inner = dict(scope, path=path, raw_path=path.encode(), query_string=b"", headers=forwarded)
        consumed = False
        async def translated_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": data, "more_body": False}
            return await receive()
        start, buffer = None, bytearray()
        state = MessageStream(model)
        async def begin(status, event):
            response_headers = [(k, v) for k, v in event.get("headers", []) if k.lower() not in {
                b"content-length", b"content-type", b"x-gateway-generation-policy"}]
            request_id = next((v for k, v in response_headers if k.lower() == b"x-request-id"), uuid4().hex.encode())
            response_headers.extend([(b"request-id", request_id), (b"anthropic-version", version),
                (b"x-gateway-model", model.encode()), (b"x-gateway-generation-policy", b"worker-defaults"),
                (b"content-type", b"text/event-stream" if stream and status == 200 else b"application/json")])
            if count:
                response_headers.append((b"x-gateway-token-count", b"estimated"))
            await send(dict(event, status=status, headers=response_headers))
        async def translated_send(event):
            nonlocal start
            if event["type"] == "http.response.start":
                start = event
                if stream and start["status"] == 200:
                    await begin(200, start)
                return
            if event["type"] != "http.response.body":
                return await send(event)
            buffer.extend(event.get("body", b""))
            if stream and start["status"] == 200:
                while b"\n\n" in buffer:
                    frame, _, rest = buffer.partition(b"\n\n")
                    buffer[:] = rest
                    payload = b"\n".join(line[5:].strip() for line in frame.splitlines() if line.startswith(b"data:"))
                    if not payload or payload == b"[DONE]":
                        continue
                    for obj in state.translate(json.loads(payload)):
                        wire = "event: " + obj["type"] + "\ndata: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
                        await send({"type": "http.response.body", "body": wire.encode(), "more_body": True})
                if not event.get("more_body", False):
                    if not state.finished:
                        obj = native_error({"message": "Upstream stream ended without a final result"}, 500)[1]
                        await send({"type": "http.response.body", "body": ("event: error\ndata: " + json.dumps(obj) + "\n\n").encode(), "more_body": True})
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
            elif not event.get("more_body", False):
                obj = json.loads(buffer)
                status = start["status"]
                if status >= 400:
                    status, obj = native_error(obj, status)
                elif not count:
                    obj = native_response(obj, model)
                await begin(status, start)
                await send({"type": "http.response.body", "body": json.dumps(obj, ensure_ascii=False).encode()})
        try:
            await self.app(inner, translated_receive, translated_send)
        except Exception:
            # Starlette's outer ServerErrorMiddleware cannot translate an
            # unhandled exception back into the native protocol for us.
            if stream and start and start["status"] == 200:
                if not state.finished:
                    obj = native_error({"message": "Claude backend could not complete the request"}, 500)[1]
                    await send({"type": "http.response.body", "body": ("event: error\ndata: " + json.dumps(obj) + "\n\n").encode(), "more_body": True})
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
            else:
                await fail("Claude backend could not complete the request", 500)
