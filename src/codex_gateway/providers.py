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
                "gemini": Capabilities()}

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
    from .client_tools import definitions, tool_outputs
    items = request.input if isinstance(request.input, list) else [request.input]
    if definitions(request) or tool_outputs(request) or any(isinstance(i, dict) and i.get("type") in {"function_call", "custom_tool_call"} for i in items):
        reject("tools", "Gemini client tool round trips have not been validated and are not enabled")
    if any(i.get("type") == "image" for i in request.worker_input()):
        reject("input", "Gemini image input is not enabled")
    for field in ("reasoning", "temperature", "top_p", "max_output_tokens", "service_tier", "truncation", "max_tool_calls", "parallel_tool_calls", "prompt_cache_retention", "include"):
        if getattr(request, field, None) is not None:
            reject(field, f"Gemini does not support the {field} parameter")
    if request.text and request.text != {"format": {"type": "text"}}:
        reject("text", "Gemini structured output and text options are not enabled")
    if request.model_extra:
        reject(next(iter(request.model_extra)), "Unrecognized Gemini parameter")


def validate_chat_capabilities(request):
    if provider_for(request.model) == "codex":
        return
    for field in ("stop", "seed", "frequency_penalty", "presence_penalty", "logit_bias",
                  "verbosity", "parallel_tool_calls", "audio", "function_call"):
        if getattr(request, field, None) is not None:
            reject(field, f"Gemini does not support the {field} parameter")
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
