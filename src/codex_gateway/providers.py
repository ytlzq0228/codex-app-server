"""Explicit registry; legacy models remain Codex unless configured otherwise."""
from dataclasses import dataclass
from fastapi import HTTPException

@dataclass(frozen=True)
class Capabilities:
    text: bool = True
    streaming: bool = True
    continuation: bool = True
    images: bool = False
    tools: bool = False
    structured_output: bool = False
    reasoning: bool = False

CAPABILITIES = {"codex": Capabilities(images=True, tools=True, structured_output=True, reasoning=True),
                "gemini": Capabilities(tools=True, structured_output=True)}

def provider_for(model):
    from .config import get_settings
    return get_settings().provider_map().get(model, "codex")

def reject(param, message, code="unsupported_parameter"):
    raise HTTPException(400, detail={"error": {"message": message, "type": "invalid_request_error", "code": code, "param": param}})

def validate_capabilities(request):
    provider = provider_for(request.model)
    if provider == "codex":
        return
    if provider not in CAPABILITIES:
        reject("model", "This provider is not enabled", "provider_unavailable")
    items = request.input if isinstance(request.input, list) else [request.input]
    # Gemini's relay currently accepts text results only, including in full history.
    from .multimodal import content_parts
    for item in items:
        if isinstance(item, dict) and item.get("type") in {"function_call_output", "custom_tool_call_output"}:
            if any(part.get("type") != "input_text" for part in content_parts(item.get("output"))):
                reject("input", "Gemini client tools currently support text results only")
    if any(i.get("type") == "image" for i in request.worker_input()):
        reject("input", "Gemini image input is not enabled")
    if request.parallel_tool_calls is True:
        reject("parallel_tool_calls", "Gemini client tools currently return one pending call at a time")
    if request.parallel_tool_calls is None:
        request.parallel_tool_calls = False
    for field in ("reasoning", "temperature", "top_p", "max_output_tokens", "service_tier", "truncation", "max_tool_calls", "prompt_cache_retention", "include"):
        if getattr(request, field, None) is not None:
            reject(field, f"Gemini does not support the {field} parameter")
    if request.text and request.text != {"format": {"type": "text"}}:
        output_schema = request.output_schema()
        if output_schema is None or set(request.text) != {"format"}:
            reject("text", "Unsupported Gemini text options")
        from jsonschema.validators import validator_for
        from jsonschema.exceptions import SchemaError
        try:
            validator_for(output_schema).check_schema(output_schema)
        except SchemaError:
            reject("text", "Invalid output JSON Schema")
        if request.tools:
            reject("text", "Combining Gemini structured output with tools is not supported")
    if request.model_extra:
        reject(next(iter(request.model_extra)), "Unrecognized Gemini parameter")


def validate_chat_capabilities(request):
    if provider_for(request.model) == "codex":
        return
    for field in ("stop", "seed", "frequency_penalty", "presence_penalty", "logit_bias",
                  "verbosity", "audio", "function_call"):
        if getattr(request, field, None) is not None:
            reject(field, f"Gemini does not support the {field} parameter")
    if request.parallel_tool_calls is True:
        reject("parallel_tool_calls", "Gemini client tools currently return one pending call at a time")
    if request.model_extra:
        reject(next(iter(request.model_extra)), "Unrecognized Gemini parameter")


async def allowed_providers(db, username):
    """Capabilities belong to the owner, shared by all their keys."""
    from sqlalchemy import select
    from .models import Worker
    return set(await db.scalars(select(Worker.provider).where(
        Worker.owner_username == username, Worker.endpoint != "removed://worker").distinct()))


async def authorize_model(db, principal, model):
    if principal.owner_username is None:
        return  # Existing development-key behavior.
    if provider_for(model) not in await allowed_providers(db, principal.owner_username):
        error = {"code": "provider_not_allowed", "message": "Your account has no Worker for this model provider", "param": "model"}
        from .audit import current_audit
        if audit := current_audit.get():
            audit["rejection"] = error
        raise HTTPException(403, detail={"error": {**error, "type": "permission_error"}})


async def visible_models(db, principal):
    from .config import get_settings
    models = get_settings().public_models()
    if principal.owner_username is None:
        return models
    providers = await allowed_providers(db, principal.owner_username)
    return [model for model in models if provider_for(model) in providers]
