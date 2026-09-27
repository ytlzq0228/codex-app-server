import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from codex_gateway import audit as audit_module
from codex_gateway.request_observation import capture_transport, request_observation


def scope(headers=(), query=b""):
    return {"type": "http", "method": "POST", "path": "/v1/responses",
            "headers": headers, "query_string": query, "scheme": "http",
            "http_version": "1.1", "client": ("127.0.0.1", 1234),
            "state": {"request_id": "req-test"}}


def test_unknown_duplicate_headers_and_query_preserved_without_credentials():
    captured = capture_transport(scope([
        (b"x-new-client-conversation", b"conversation-a"),
        (b"x-new-client-conversation", b"conversation-b"),
        (b"authorization", b"Bearer secret-one"),
        (b"x-auth", b"secret-two"),
        (b"cookie", b"session=secret-three"),
        (b"x-api-key", b"secret-four"),
        (b"x-codex-turn-metadata", b'{"thread_id":"thread-a","access_token":"secret-five"}'),
        (b"referer", b"https://user:secret-six@example.com/chat?session_id=a&key=secret-seven#secret-eight"),
    ], b"session_id=a&session_id=b&api_key=secret-nine"))
    serialized = json.dumps(captured)
    assert "secret-" not in serialized
    assert [h["value"] for h in captured["headers"][:2]] == ["conversation-a", "conversation-b"]
    assert json.loads(captured["headers"][6]["value"])["thread_id"] == "thread-a"
    assert captured["query_params"][:2] == [{"name": "session_id", "value": "a"}, {"name": "session_id", "value": "b"}]
    assert captured["gateway_request_id"] == "req-test"


def test_capture_limits_and_malformed_structured_values():
    captured = capture_transport(scope([(b"x-metadata", b'{"secret": "oops"'),
                                        (b"x-large", b"a" * 9000)] + [(b"x-id", b"v")] * 200))
    assert "oops" not in json.dumps(captured)
    assert captured["headers"][1]["value"] == "[OMITTED: VALUE TOO LONG]"
    assert captured["capture_notes"]
    assert capture_transport(scope(query=b"a=b&" * 200))["capture_notes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("peer,chain,expected,scheme", [
    ("<pfsense-a1>", "198.51.100.7", "198.51.100.7", "https"),
    ("<pfsense-a2>", "192.0.2.99, 198.51.100.7", "198.51.100.7", "https"),
    ("<pfsense-b1>", "198.51.100.7, <pfsense-a1>", "198.51.100.7", "https"),
    ("<pfsense-b2>", "198.51.100.7", "198.51.100.7", "https"),
    ("198.51.100.8", "192.0.2.99", "198.51.100.8", "http"),
    ("172.19.0.1", "192.0.2.99", "172.19.0.1", "http"),
])
async def test_proxy_trust_is_applied_before_observation(peer, chain, expected, scheme):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    captured = []

    async def app(scope, receive, send):
        captured.append(capture_transport(scope))

    request_scope = scope([(b"x-forwarded-for", chain.encode()), (b"x-forwarded-proto", b"https")])
    request_scope["client"] = (peer, 1234)
    middleware = ProxyHeadersMiddleware(app, trusted_hosts="192.0.2.8,192.0.2.9,203.0.113.8,203.0.113.9")
    await middleware(request_scope, None, None)
    assert captured[0]["client_ip"] == expected
    assert captured[0]["client_address"][0] == expected
    assert captured[0]["scheme"] == scheme
    assert captured[0]["headers"][0]["value"] == chain


@pytest.mark.asyncio
@pytest.mark.parametrize("saved,status,complete", [(True, 200, True), (False, 400, True), (False, 200, False)])
async def test_middleware_preserves_stream_and_records_success_failure_disconnect(monkeypatch, saved, status, complete):
    records = []

    class DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def add(self, record):
            records.append(record)

        async def commit(self):
            pass

    monkeypatch.setattr(audit_module, "SessionLocal", DB)
    monkeypatch.setattr(audit_module, "get_settings", lambda: SimpleNamespace(max_request_bytes=10000))
    body = b'{"model":"test","input":"hello","new_client_field":"session-a"}'
    incoming = [{"type": "http.request", "body": body[:10], "more_body": True},
                {"type": "http.request", "body": body[10:], "more_body": False}]
    outgoing = [{"type": "http.response.start", "status": status, "headers": []},
                {"type": "http.response.body", "body": b"data: unchanged\n\n", "more_body": not complete}]
    captured = []
    received = []
    sent = []

    async def app(scope, receive, send):
        audit = audit_module.current_audit.get()
        audit["principal"] = SimpleNamespace(key_id=None, owner_username="test")
        received.extend([await receive(), await receive()])
        captured.append(request_observation(audit))
        assert audit_module.request_params(audit)["new_client_field"] == "session-a"
        audit["saved"] = saved
        for message in outgoing:
            await send(message)

    iterator = iter(incoming)

    async def receive():
        return next(iterator)

    async def send(message):
        sent.append(message)

    await audit_module.RequestAuditMiddleware(app)(scope(), receive, send)
    assert received == incoming and sent == outgoing
    assert captured[0]["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert captured[0]["body_bytes_received"] == len(body)
    assert captured[0]["body_complete"]
    assert len(records) == (0 if saved else 1)
    if records:
        assert records[0].request_observation == captured[0]
        assert records[0].status_code == (status if complete else 499)
    assert audit_module.current_audit.get() is None


@pytest.mark.asyncio
async def test_success_usage_persists_observation_and_original_body(monkeypatch):
    from codex_gateway import main
    from codex_gateway.auth import ApiPrincipal
    from codex_gateway.backend import BackendTarget
    from codex_gateway.schemas import BackendResult

    records = []

    class DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args):
            return None

        def add(self, record):
            records.append(record)

        async def commit(self):
            pass

    monkeypatch.setattr(main, "SessionLocal", DB)
    original = {"model": "test", "input": "hello", "client_metadata": {"thread_id": "client-thread"}}
    body = json.dumps(original).encode()
    audit = {"body": body, "transport": capture_transport(scope()), "body_hash": hashlib.sha256(body),
             "body_bytes_received": len(body), "body_complete": True, "saved": False}
    token = audit_module.current_audit.set(audit)
    try:
        await main.save_usage("resp-test", ApiPrincipal(None, "test"),
                              BackendTarget("test", "ws://test", "/workspace"), "test", 200,
                              time.monotonic(), BackendResult(text="hello", thread_id="backend-thread"))
    finally:
        audit_module.current_audit.reset(token)
    assert records[0].request_params == original
    assert records[0].request_observation == request_observation(audit)
    assert records[0].thread_id == "backend-thread"
    assert audit["saved"]
