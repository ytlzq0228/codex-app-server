import pytest

from codex_gateway.backend import _token_counts
from codex_gateway.schemas import ResponseRequest
from codex_gateway.security import generate_api_key, hash_api_key, keys_equal


def test_key_hashing() -> None:
    raw, prefix = generate_api_key()
    assert raw.startswith(f"cag_{prefix}_")
    digest = hash_api_key(raw, "pepper")
    assert keys_equal(raw, digest, "pepper")
    assert not keys_equal(raw + "x", digest, "pepper")


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
