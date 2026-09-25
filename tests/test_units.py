from uuid import uuid4

import pytest

import codex_gateway.main as main_module
from codex_gateway.auth import ApiPrincipal
from codex_gateway.backend import BackendTarget, WorkerFailure, _token_counts, classify_worker_failure
from codex_gateway.main import complete_with_failover
from codex_gateway.schemas import BackendResult, ResponseRequest
from codex_gateway.security import generate_api_key, hash_api_key, hash_password, keys_equal, verify_password


def test_key_hashing() -> None:
    raw, prefix = generate_api_key()
    assert raw.startswith(f"cag_{prefix}_")
    digest = hash_api_key(raw, "pepper")
    assert keys_equal(raw, digest, "pepper")
    assert not keys_equal(raw + "x", digest, "pepper")


def test_admin_password_hashing() -> None:
    encoded = hash_password("a secure admin password", iterations=1_000)
    assert verify_password("a secure admin password", encoded)
    assert not verify_password("wrong password", encoded)


def test_input_messages_are_flattened() -> None:
    request = ResponseRequest(model="codex", instructions="be terse", input=[{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}])
    assert request.input_text() == "be terse\n\nUSER:\nhello"


def test_empty_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        ResponseRequest(model="codex", input="  ")


@pytest.mark.parametrize(("payload", "expected"), [
    ({"tokenUsage": {"total": {"inputTokens": 12, "outputTokens": 3}}}, (12, 3)),
    ({"usage": {"input_tokens": 4, "output_tokens": 2}}, (4, 2)),
])
def test_token_usage_variants(payload: dict, expected: tuple[int, int]) -> None:
    assert _token_counts(payload) == expected


@pytest.mark.parametrize(("message", "kind"), [
    ("Codex worker is not logged in", "logged_out"),
    ("You have reached your 5 hour usage limit", "limit"),
    ("HTTP 429 too many requests", "limit"),
    ("websocket closed unexpectedly", "connection"),
])
def test_worker_failure_classification(message: str, kind: str) -> None:
    assert classify_worker_failure(message) == kind


@pytest.mark.asyncio
async def test_safe_failure_retries_once_on_another_worker(monkeypatch) -> None:
    first_id, second_id = uuid4(), uuid4()
    first = BackendTarget("first", "ws://first", "/workspace/key", first_id)
    second = BackendTarget("second", "ws://second", "/workspace/key", second_id)
    calls: list = []
    quarantined: list = []

    class Backend:
        async def complete(self, _body, target):
            calls.append(target.worker_id)
            if target.worker_id == first_id:
                raise WorkerFailure("connection refused", safe_to_retry=True)
            return BackendResult(text="ok", thread_id="thread", input_tokens=1, output_tokens=1)

    async def fake_quarantine(worker_id, reason, kind):
        quarantined.append((worker_id, kind))

    async def fake_retry(_principal, _target):
        return second

    monkeypatch.setattr(main_module, "quarantine_worker", fake_quarantine)
    monkeypatch.setattr(main_module, "retry_target", fake_retry)
    result, selected = await complete_with_failover(ResponseRequest(model="codex", input="hello"), Backend(), ApiPrincipal(None, "test"), first, allow_retry=True)
    assert result.text == "ok"
    assert selected.worker_id == second_id
    assert calls == [first_id, second_id]
    assert quarantined == [(first_id, "connection")]


@pytest.mark.asyncio
async def test_ambiguous_started_turn_is_never_replayed(monkeypatch) -> None:
    worker_id = uuid4()
    target = BackendTarget("first", "ws://first", "/workspace/key", worker_id)
    retry_called = False

    class Backend:
        async def complete(self, _body, _target):
            raise WorkerFailure("connection lost after turn/start", safe_to_retry=False)

    async def fake_quarantine(*_args):
        return None

    async def fake_retry(*_args):
        nonlocal retry_called
        retry_called = True

    monkeypatch.setattr(main_module, "quarantine_worker", fake_quarantine)
    monkeypatch.setattr(main_module, "retry_target", fake_retry)
    with pytest.raises(WorkerFailure):
        await complete_with_failover(ResponseRequest(model="codex", input="hello"), Backend(), ApiPrincipal(None, "test"), target, allow_retry=True)
    assert retry_called is False
