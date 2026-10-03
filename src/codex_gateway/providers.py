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
                "claude": Capabilities(images=True, tools=True, structured_output=True, reasoning=True),
                "gemini": Capabilities(images=True, tools=True, structured_output=True)}

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
    if provider == "claude":
        return _validate_claude(request)
    items = request.input if isinstance(request.input, list) else [request.input]
    # Gemini's relay currently accepts text results only, including in full history.
    from .multimodal import content_parts
    for item in items:
        if isinstance(item, dict) and item.get("type") in {"function_call_output", "custom_tool_call_output"}:
            if any(part.get("type") != "input_text" for part in content_parts(item.get("output"))):
                reject("input", "Gemini client tools currently support text results only")
    # Permission to parallelize does not require parallel execution. The Gemini
    # relay continues to deliver one pending call at a time.
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
    if provider_for(request.model) == "claude":
        for field in ("seed", "frequency_penalty", "presence_penalty", "logit_bias", "verbosity", "audio", "function_call"):
            if getattr(request, field, None) is not None:
                reject(field, f"Claude does not support the {field} parameter")
        request.parallel_tool_calls = False
        if request.model_extra:
            reject(next(iter(request.model_extra)), "Unrecognized Claude parameter")
        return
    for field in ("stop", "seed", "frequency_penalty", "presence_penalty", "logit_bias",
                  "verbosity", "audio", "function_call"):
        if getattr(request, field, None) is not None:
            reject(field, f"Gemini does not support the {field} parameter")
    request.parallel_tool_calls = False
    if request.model_extra:
        reject(next(iter(request.model_extra)), "Unrecognized Gemini parameter")


def _validate_claude(request):
    request.parallel_tool_calls = False
    if request.reasoning is not None:
        if set(request.reasoning) - {"effort"} or request.reasoning.get("effort") not in {"low", "medium", "high", "xhigh", "max"}:
            reject("reasoning", "Claude effort must be low, medium, high, xhigh or max")
    if request.text and request.text != {"format": {"type": "text"}}:
        schema = request.output_schema()
        if schema is None or set(request.text) != {"format"}:
            reject("text", "Unsupported Claude text options")
        from jsonschema.validators import validator_for
        from jsonschema.exceptions import SchemaError
        try:
            validator_for(schema).check_schema(schema)
        except SchemaError:
            reject("text", "Invalid output JSON Schema")
        from .client_tools import definitions
        if definitions(request):
            reject("text", "Combining Claude structured output with tools is not supported")
    if request.model_extra:
        reject(next(iter(request.model_extra)), "Unrecognized Claude parameter")


def entitlement_filters():
    """A Worker grants its provider only while logged in and serving.

    Logout, deletion, disabling or any failure revokes the grant; a usage-limit
    pause does not, matching the contribution rule for paid accounts.
    """
    from sqlalchemy import and_, or_
    from .models import Worker, WorkerStatus
    return [Worker.enabled.is_(True), Worker.endpoint != "removed://worker",
            Worker.auth_mode.is_not(None), Worker.account_checked_at.is_not(None),
            or_(Worker.failure_kind.is_(None), Worker.failure_kind != "logged_out"),
            or_(Worker.status.in_([WorkerStatus.ready, WorkerStatus.busy, WorkerStatus.draining]),
                and_(Worker.status == WorkerStatus.error, Worker.failure_kind == "limit"))]


async def allowed_providers(db, username):
    """Capabilities belong to the owner, shared by all their keys."""
    from sqlalchemy import select
    from .models import User, Worker
    automatic = set(await db.scalars(select(Worker.provider).where(
        Worker.owner_username == username, *entitlement_filters()).distinct()))
    grants = await db.scalar(select(User.provider_grants).where(User.username == username))
    return automatic | (set(grants or []) & CAPABILITIES.keys())


async def authorize_model(db, principal, model):
    if principal.owner_username is None:
        return  # Existing development-key behavior.
    if provider_for(model) not in await allowed_providers(db, principal.owner_username):
        error = {"code": "provider_not_allowed", "message": "Your account is not enabled for this model provider", "param": "model"}
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
