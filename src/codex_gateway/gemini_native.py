"""Gemini native wire adapter over the existing authenticated Chat pipeline.

Subscription workers choose sampling/reasoning themselves. Native CLI defaults
are advisory; they are not forwarded as unsupported OpenAI generation options.
"""
import base64
import json
import logging
import re

from .config import get_settings

_ROUTE = re.compile(r"^/v1beta/models/([^/:]+):(generateContent|streamGenerateContent)$")
_SIGNATURE = "gateway-call:"


def schema(value):
    if isinstance(value, list):
        return [schema(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, val in value.items():
        if key == "type" and isinstance(val, str):
            out[key] = val.lower()
        elif key == "properties":
            out[key] = {name: schema(spec) for name, spec in val.items()}
        elif key not in {"propertyOrdering", "nullable"}:
            out[key] = schema(val)
    if value.get("nullable"):
        return {"anyOf": [out, {"type": "null"}]}
    return out


def call_id(part):
    call = part["functionCall"]
    if call.get("id"):
        return call["id"]
    try:
        decoded = base64.b64decode(part.get("thoughtSignature", ""), validate=True).decode()
        if decoded.startswith(_SIGNATURE):
            return decoded[len(_SIGNATURE):]
    except (ValueError, UnicodeError):
        pass
    raise ValueError("Function call history must preserve its id or thoughtSignature")


def translate_request(body, model, stream):
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object")
    unsupported = set(body) - {"contents", "systemInstruction", "tools", "toolConfig",
                                "generationConfig", "labels", "sessionId", "safetySettings"}
    if unsupported:
        raise ValueError("Unsupported Gemini fields: " + ", ".join(sorted(unsupported)))
    config = body.get("generationConfig") or {}
    unsupported_config = set(config) - {
        "temperature", "topP", "topK", "maxOutputTokens", "thinkingConfig",
        "candidateCount", "responseSchema", "responseJsonSchema", "responseMimeType", "responseModalities",
    }
    if unsupported_config:
        raise ValueError("Unsupported generationConfig fields: " + ", ".join(sorted(unsupported_config)))
    mime = config.get("responseMimeType", "text/plain")
    output_schema = config.get("responseJsonSchema", config.get("responseSchema"))
    if mime not in {"text/plain", "application/json"}:
        raise ValueError("Only text/plain and application/json output are supported")
    if output_schema is not None and mime != "application/json":
        raise ValueError("Response schema requires application/json")
    if "responseSchema" in config and "responseJsonSchema" in config:
        raise ValueError("Specify only one response schema")
    if config.get("candidateCount", 1) != 1:
        raise ValueError("Only one candidate is supported")
    if config.get("responseModalities", ["TEXT"]) != ["TEXT"]:
        raise ValueError("Only text output is supported")
    if body.get("safetySettings"):
        raise ValueError("Safety setting overrides are not supported")
    messages, pending = [], {}
    instruction = body.get("systemInstruction") or {}
    if instruction:
        parts = instruction.get("parts", [])
        if any(set(p) - {"text"} for p in parts):
            raise ValueError("Only text system instructions are supported")
        messages.append({"role": "system", "content": "\n".join(p.get("text", "") for p in parts)})
    contents = body.get("contents")
    if not isinstance(contents, list) or not contents:
        raise ValueError("contents must be a nonempty list")
    for content in contents:
        role = content.get("role", "user")
        if role not in {"user", "model"}:
            raise ValueError("Content role must be user or model")
        text, calls, results = [], [], []
        seen_results = {}
        for part in content.get("parts", []):
            if set(part) - {"text", "thought", "thoughtSignature", "functionCall", "functionResponse", "inlineData", "fileData"}:
                raise ValueError("Unsupported content part fields")
            if ("inlineData" in part or "fileData" in part) and len(set(part) & {"text", "inlineData", "fileData", "functionCall", "functionResponse"}) != 1:
                raise ValueError("An image part must contain exactly one content source")
            if "functionCall" in part:
                if role != "model":
                    raise ValueError("functionCall requires model role")
                c = part["functionCall"]
                cid = call_id(part)
                pending.setdefault(c["name"], []).append(cid)
                calls.append({"id": cid, "type": "function", "function": {
                    "name": c["name"], "arguments": json.dumps(c.get("args", {}))}})
            elif "functionResponse" in part:
                # agy gateway mode groups functionResponse parts under model role.
                # Bind by call id/name, never by this transport role label.
                r = part["functionResponse"]
                if set(r) - {"id", "name", "response"}:
                    raise ValueError("Only complete text function responses are supported")
                ids = pending.get(r["name"], [])
                cid = r.get("id") or (ids[0] if ids else None)
                identity = (r["name"], cid)
                if cid and identity in seen_results:
                    if seen_results[identity] == r.get("response", {}):
                        continue
                    raise ValueError("Conflicting duplicate function response")
                if not cid or cid not in ids:
                    raise ValueError("Function response has no matching call in history")
                seen_results[identity] = r.get("response", {})
                ids.remove(cid)
                results.append({"role": "tool", "tool_call_id": cid,
                                "content": json.dumps(r.get("response", {}), ensure_ascii=False)})
            elif "inlineData" in part or "fileData" in part:
                if role != "user":
                    raise ValueError("Images require user role")
                if "inlineData" in part and "fileData" in part:
                    raise ValueError("Specify only one image source")
                source = part.get("inlineData", part.get("fileData"))
                if not isinstance(source, dict) or source.get("mimeType") not in {
                        "image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}:
                    raise ValueError("Only PNG, JPEG, WEBP and GIF image parts are supported")
                if "inlineData" in part:
                    if set(source) != {"mimeType", "data"} or not isinstance(source["data"], str):
                        raise ValueError("inlineData requires mimeType and base64 data")
                    url = "data:" + source["mimeType"] + ";base64," + source["data"]
                else:
                    if set(source) != {"mimeType", "fileUri"} or not isinstance(source["fileUri"], str) or not source["fileUri"].startswith(("https://", "http://")):
                        raise ValueError("fileData requires a public HTTP(S) image URL")
                    url = source["fileUri"]
                from .multimodal import image_part
                image_part({"type": "input_image", "image_url": url})
                text.append({"type": "image_url", "image_url": {"url": url}})
            elif "text" in part:
                if not part.get("thought"):
                    text.append(part["text"])
            else:
                raise ValueError("Only text and function parts are supported")
        if text or calls:
            message = {"role": "assistant" if role == "model" else "user",
                       "content": ([{"type": "text", "text": p} if isinstance(p, str) else p for p in text]
                                   if any(isinstance(p, dict) for p in text) else "\n".join(text) or None)}
            if calls:
                message["tool_calls"] = calls
            messages.append(message)
        messages.extend(results)
    tools = []
    for group in body.get("tools") or []:
        if set(group) != {"functionDeclarations"}:
            raise ValueError("Only client functionDeclarations tools are supported")
        for tool in group["functionDeclarations"]:
            tools.append({"type": "function", "function": {
                "name": tool["name"], "description": tool.get("description", ""),
                "parameters": schema(tool.get("parametersJsonSchema", tool.get("parameters", {"type": "object", "properties": {}})))}})
    function_config = (body.get("toolConfig") or {}).get("functionCallingConfig", {})
    mode = function_config.get("mode", "AUTO")
    if mode not in {"AUTO", "NONE"} or function_config.get("allowedFunctionNames"):
        raise ValueError("Forced function calling is not supported")
    result = {"model": model, "messages": messages, "tools": tools,
              "tool_choice": "none" if mode == "NONE" else "auto",
              "parallel_tool_calls": False, "stream": stream}
    if mime == "application/json":
        result["response_format"] = ({"type": "json_schema", "json_schema": {
            "name": "native_response", "schema": schema(output_schema)}}
            if output_schema is not None else {"type": "json_object"})
    if stream:
        result["stream_options"] = {"include_usage": True}
    return result


def usage(value):
    return {"promptTokenCount": value.get("prompt_tokens", 0),
            "candidatesTokenCount": value.get("completion_tokens", 0),
            "totalTokenCount": value.get("total_tokens", 0),
            "cachedContentTokenCount": (value.get("prompt_tokens_details") or {}).get("cached_tokens", 0)}


def function_part(call):
    return {"functionCall": {"id": call["id"], "name": call["function"]["name"],
                             "args": json.loads(call["function"]["arguments"])},
            "thoughtSignature": base64.b64encode((_SIGNATURE + call["id"]).encode()).decode()}


def native_error(value, status=400):
    error = value.get("error", value)
    if not isinstance(error, dict):
        error = {"message": str(error)}
    code = status if status >= 400 else 502
    return {"error": {"code": code, "status": {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED",
                     403: "PERMISSION_DENIED", 404: "NOT_FOUND", 429: "RESOURCE_EXHAUSTED",
                     503: "UNAVAILABLE"}.get(code, "INTERNAL"),
                     "message": error.get("message", str(error)),
                     "details": [{"reason": str(error.get("code", "gateway_error"))}]}}


def native_response(value, model):
    if "error" in value:
        return native_error(value)
    message = value["choices"][0]["message"]
    parts = ([{"text": message["content"]}] if message.get("content") else [])
    parts += [function_part(c) for c in message.get("tool_calls", [])]
    return {"candidates": [{"index": 0, "content": {"role": "model", "parts": parts},
                             "finishReason": "STOP"}], "modelVersion": model,
            "usageMetadata": usage(value.get("usage") or {})}


class GeminiNativeMiddleware:
    """Translate at the boundary so quota, key isolation, audit and cleanup are shared."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        match = _ROUTE.match(scope.get("path", "")) if scope["type"] == "http" else None
        if not match or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        requested_model, action = match.groups()
        stream = action == "streamGenerateContent"
        settings = get_settings()
        raw = bytearray()
        async def fail(message, status=400):
            data = json.dumps(native_error({"error": {"message": message}}, status)).encode()
            await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": data})
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            raw.extend(event.get("body", b""))
            if len(raw) > settings.max_request_bytes:
                return await fail("Request body is too large", 413)
            if not event.get("more_body", False):
                break
        try:
            aliases = dict(entry.strip().split(":", 1) for entry in settings.gemini_native_model_aliases.split(",") if entry.strip())
            model = aliases.get(requested_model, requested_model)
            body = translate_request(json.loads(raw), model, stream)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            logging.getLogger(__name__).warning("Gemini native request rejected: %s", exc)
            return await fail(str(exc))
        data = json.dumps(body).encode()
        headers = [(k, v) for k, v in scope["headers"] if k.lower() not in {b"content-length", b"x-goog-api-key"}]
        api_key = next((v for k, v in scope["headers"] if k.lower() == b"x-goog-api-key"), None)
        if api_key and not any(k.lower() == b"authorization" for k, _ in headers):
            headers.append((b"authorization", b"Bearer " + api_key))
        headers.append((b"content-length", str(len(data)).encode()))
        inner = dict(scope, path="/v1/chat/completions", raw_path=b"/v1/chat/completions",
                     query_string=b"", headers=headers)
        consumed = False
        async def translated_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": data, "more_body": False}
            return await receive()
        status, buffer = 200, bytearray()
        async def translated_send(event):
            nonlocal status
            if event["type"] == "http.response.start":
                status = event["status"]
                response_headers = [(k, v) for k, v in event.get("headers", []) if k.lower() not in {b"content-length", b"content-type"}]
                response_headers.append((b"content-type", b"text/event-stream" if stream and status == 200 else b"application/json"))
                # Expose the actual configured model when a native alias is used.
                response_headers.append((b"x-gateway-model", model.encode()))
                response_headers.append((b"x-gateway-generation-policy", b"worker-defaults"))
                await send(dict(event, headers=response_headers))
                return
            if event["type"] != "http.response.body":
                return await send(event)
            buffer.extend(event.get("body", b""))
            if stream and status == 200:
                while b"\n\n" in buffer:
                    frame, _, rest = buffer.partition(b"\n\n")
                    buffer[:] = rest
                    if not frame.startswith(b"data:"):
                        continue
                    payload = frame[5:].strip()
                    if payload == b"[DONE]":
                        continue
                    obj = json.loads(payload)
                    native = {"modelVersion": model}
                    if "error" in obj:
                        code = obj["error"].get("code", "")
                        native = native_error(obj, 400 if code.startswith("invalid_") or code.startswith("client_tool") else 503 if code == "worker_capacity_exceeded" else 429 if code == "provider_quota_exhausted" else 502)
                    elif obj.get("usage") is not None:
                        native["usageMetadata"] = usage(obj["usage"])
                    else:
                        choice = obj["choices"][0]
                        delta = choice.get("delta", {})
                        parts = ([{"text": delta["content"]}] if delta.get("content") else [])
                        parts += [function_part(c) for c in delta.get("tool_calls", [])]
                        if not parts and not choice.get("finish_reason"):
                            continue
                        candidate = {"index": 0, "content": {"role": "model", "parts": parts}}
                        if choice.get("finish_reason"):
                            candidate["finishReason"] = "STOP"
                        native["candidates"] = [candidate]
                    await send({"type": "http.response.body", "body": b"data: " + json.dumps(native).encode() + b"\n\n", "more_body": True})
                if not event.get("more_body", False):
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
            elif not event.get("more_body", False):
                obj = json.loads(buffer)
                native = native_error(obj, status) if status >= 400 else native_response(obj, model)
                await send({"type": "http.response.body", "body": json.dumps(native).encode()})
        await self.app(inner, translated_receive, translated_send)
