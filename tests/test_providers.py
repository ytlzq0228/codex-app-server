from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException
from codex_gateway.config import get_settings
from codex_gateway.providers import provider_for, validate_capabilities
from codex_gateway.schemas import ResponseRequest
from codex_gateway.backend import BackendTarget
from codex_gateway.auth import ApiPrincipal
from codex_gateway import main

@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setattr(get_settings(), "model_providers", "gemini-test:gemini")

def test_registry_preserves_legacy(gemini):
    assert provider_for("gpt-6-sol") == "codex"
    assert provider_for("gemini-test") == "gemini"
    assert BackendTarget("k", "ws://w", "/workspace").provider == "codex"

@pytest.mark.parametrize("extra,param", [
    ({"reasoning": {"effort": "high"}}, "reasoning"),
    ({"text": {"format": {"type": "unsupported"}}}, "text"),
    ({"temperature": 0}, "temperature"),
    ({"unknown_option": True}, "unknown_option"),
    ({"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "https://example.org/a.png"}]}]}, "input"),
])
def test_gemini_rejects_unsupported(gemini, extra, param):
    with pytest.raises(HTTPException) as exc:
        validate_capabilities(ResponseRequest(**({"model": "gemini-test", "input": "hello"} | extra)))
    assert exc.value.detail["error"]["param"] == param

def test_text_and_continuation_supported(gemini):
    validate_capabilities(ResponseRequest(model="gemini-test", input="hello", previous_response_id="resp_test"))


@pytest.mark.parametrize("parallel", [None, False, True])
@pytest.mark.parametrize("model", ["gemini-test", "gpt-6-sol"])
def test_parallel_permission_keeps_gemini_serial_and_codex_unchanged(gemini, parallel, model):
    from codex_gateway.schemas import ChatCompletionRequest
    from codex_gateway.providers import validate_chat_capabilities
    response = ResponseRequest(model=model, input="hello", parallel_tool_calls=parallel)
    chat = ChatCompletionRequest(model=model, messages=[{"role": "user", "content": "hello"}],
                                 parallel_tool_calls=parallel)
    validate_capabilities(response)
    validate_chat_capabilities(chat)
    expected = False if model == "gemini-test" else parallel
    assert response.parallel_tool_calls is expected
    assert chat.parallel_tool_calls is expected

@pytest.mark.asyncio
async def test_pool_never_crosses_provider(gemini):
    workers = [SimpleNamespace(id=uuid4(), provider=p, enabled=True, status="ready", container_name=p,
                              endpoint=f"http://{p}", execution_generation=0) for p in ("codex", "gemini")]
    class Session:
        async def scalars(self, *args): return SimpleNamespace(all=lambda: workers)
        async def scalar(self, *args): return None
        async def get(self, model, key): return next(w for w in workers if w.id == key)
    principal = ApiPrincipal(uuid4(), "test")
    target = await main.choose_target(principal, Session(), provider="gemini")
    assert target.worker_id == workers[1].id
    assert target.provider == "gemini"
    with pytest.raises(HTTPException):
        await main.choose_target(principal, Session(), provider="gemini", exclude_worker_ids={workers[1].id})
    with pytest.raises(HTTPException) as exc:
        await main.choose_target(principal, Session(), binding=SimpleNamespace(provider="codex", worker_id=workers[0].id), provider="gemini")
    assert exc.value.detail["error"]["code"] == "provider_mismatch"


@pytest.mark.parametrize("field,value", [("stop", "END"), ("seed", 1), ("frequency_penalty", 0.5),
                                        ("presence_penalty", 0.5), ("logit_bias", {"1": 1}), ("verbosity", "low"),
                                        ("audio", {})])
def test_chat_options_are_not_lost_in_conversion(gemini, field, value):
    from codex_gateway.schemas import ChatCompletionRequest
    from codex_gateway.providers import validate_chat_capabilities
    request = ChatCompletionRequest(model="gemini-test", messages=[{"role": "user", "content": "hello"}], **{field: value})
    with pytest.raises(HTTPException) as exc:
        validate_chat_capabilities(request)
    assert exc.value.detail["error"]["param"] == field
