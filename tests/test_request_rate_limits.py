import asyncio
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from codex_gateway import main, request_rate_limits as limits
from codex_gateway.auth import ApiPrincipal
from codex_gateway.backend import BackendTarget, WorkerFailure
from codex_gateway.models import RequestRateWindow
from codex_gateway.schemas import ResponseRequest, ChatCompletionRequest


def test_rolling_window_and_rejected_retries():
    stamps = []
    for instant in [59.9, 60.1, 60.2]:
        stamps, wait = limits.record_attempt(stamps, instant, 3)
        assert wait == 0
    stamps, wait = limits.record_attempt(stamps, 60.3, 3)
    assert wait == 60  # No fresh allowance at the calendar-minute boundary.
    stamps, wait = limits.record_attempt(stamps, 119.9, 3)
    assert wait == 1  # The rejected attempt was retained.
    stamps, wait = limits.record_attempt(stamps, 180, 3)
    assert wait == 0 and stamps == [180]


@pytest.mark.parametrize("path,payload", [
    ("/v1/responses", {"input": "hello"}),
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hello"}]}),
])
@pytest.mark.parametrize("stream", [False, True])
def test_http_failed_calls_count_and_return_429_before_stream(monkeypatch, path, payload, stream):
    from fastapi.testclient import TestClient
    attempts = []
    async def count(_):
        attempts.append(1)
        if len(attempts) > 2:
            raise HTTPException(429, {"error": {"code": "key_rate_limit_exceeded",
                "type": "rate_limit_error", "message": "RATE LIMIT"}}, headers={"Retry-After": "60"})
    monkeypatch.setattr(limits, "count_key_attempt", count)
    monkeypatch.setattr(main, "model_available", AsyncMock(return_value=False))
    with TestClient(main.app) as client:
        kwargs = dict(headers={"Authorization": "Bearer cag_dev_local"},
                      json={"model": "unavailable-test-model", "stream": stream, **payload})
        assert client.post(path, **kwargs).status_code == 400
        assert client.post(path, **kwargs).status_code == 400
        response = client.post(path, **kwargs)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
        assert response.json()["error"]["type"] == "rate_limit_error"
        assert len(attempts) == 3


@pytest.mark.parametrize("path,payload", [
    ("/v1/responses", {"input": "hello"}),
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hello"}]}),
    ("/v1/messages", {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 64}),
])
@pytest.mark.parametrize("stream", [False, True])
def test_initial_worker_limit_is_http_429(monkeypatch, path, payload, stream):
    from fastapi.testclient import TestClient
    settings = main.get_settings()
    monkeypatch.setattr(settings, "allowed_models", "gpt-6-sol,claude-sonnet-4-6")
    counter = AsyncMock(side_effect=HTTPException(429, {"error": {
        "code": "worker_rate_limit_exceeded", "type": "rate_limit_error", "message": "RATE LIMIT"}},
        headers={"Retry-After": "42"}))
    target = BackendTarget("test", "http://example.test", "/workspace", uuid4())
    monkeypatch.setattr(main, "choose_execution_target", AsyncMock(return_value=target))
    monkeypatch.setattr(main, "count_worker_attempt", counter)
    monkeypatch.setattr(main, "prepare_execution", AsyncMock(side_effect=lambda body, *a, **kw: (body, None)))
    with TestClient(main.app) as client:
        response = client.post(path, headers={"Authorization": "Bearer cag_dev_local", "anthropic-version": "2023-06-01"},
                               json={"model": "claude-sonnet-4-6" if path == "/v1/messages" else "gpt-6-sol",
                                     "stream": stream, **payload})
        assert response.status_code == 429, response.text
        assert response.headers["Retry-After"] == "42"
        assert response.json()["error"]["type"] == "rate_limit_error"
        counter.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["complete", "responses", "chat"])
@pytest.mark.parametrize("scope", ["key", "worker"])
async def test_failover_is_charged_and_limit_stops_retry(monkeypatch, mode, scope):
    principal = ApiPrincipal(uuid4(), "test")
    target = BackendTarget("first", "http://example.test", "/workspace", uuid4())
    replacement = BackendTarget("second", "http://example.test", "/workspace", uuid4())
    calls = []
    async def charge_key(_):
        calls.append("key")
        if scope == "key":
            raise HTTPException(429, {"error": {"code": "key_rate_limit_exceeded",
                                               "type": "rate_limit_error", "message": "RATE LIMIT"}})
    async def charge_worker(_):
        calls.append("worker")
        raise HTTPException(429, {"error": {"code": "worker_rate_limit_exceeded",
                                           "type": "rate_limit_error", "message": "RATE LIMIT"}})
    @asynccontextmanager
    async def session():
        yield None
    monkeypatch.setattr(main, "SessionLocal", session)
    monkeypatch.setattr(main, "count_key_attempt", charge_key)
    monkeypatch.setattr(main, "count_worker_attempt", charge_worker)
    monkeypatch.setattr(main, "choose_target", AsyncMock(return_value=replacement))
    monkeypatch.setattr(main, "verify_worker_failure", AsyncMock())
    monkeypatch.setattr(main, "save_usage", AsyncMock())
    failure = WorkerFailure("connection failed", safe_to_retry=True)
    backend = SimpleNamespace(complete=AsyncMock(side_effect=failure))
    async def stream(*args):
        calls.append("backend")
        raise failure
        yield
    backend.stream = stream
    body = ResponseRequest(model="test", input="hello")
    if mode == "complete":
        with pytest.raises(HTTPException) as error:
            await main.complete_with_failover(body, backend, principal, target, allow_retry=True)
        assert error.value.status_code == 429
        assert backend.complete.await_count == 1
    else:
        iterator = main.response_stream(body, backend, principal, target, None) if mode == "responses" else main.chat_completion_stream(
            ChatCompletionRequest(model="test", messages=[{"role": "user", "content": "hello"}]),
            body, backend, principal, target)
        wire = "".join([part async for part in iterator])
        assert scope + "_rate_limit_exceeded" in wire and "rate_limit_error" in wire
        assert calls.count("backend") == 1
        assert main.save_usage.await_args.args[4] == 429
    assert calls.count("key") == 1
    assert calls.count("worker") == (scope == "worker")


@pytest.mark.asyncio
async def test_postgres_concurrent_admission_and_committed_rejections(monkeypatch):
    dsn = os.environ.get("RATE_LIMIT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("requires disposable RATE_LIMIT_TEST_DATABASE_URL")
    engine = create_async_engine(dsn)
    # Separate pools model two gateway processes sharing PostgreSQL.
    other_engine = create_async_engine(dsn)
    factories = [async_sessionmaker(e, expire_on_commit=False) for e in (engine, other_engine)]
    scope = "key:test-" + uuid4().hex
    async with engine.begin() as conn:
        await conn.run_sync(RequestRateWindow.__table__.create, checkfirst=True)
    counter = 0
    def session():
        nonlocal counter
        counter += 1
        return factories[counter % 2]()
    monkeypatch.setattr(limits, "SessionLocal", session)
    try:
        results = await asyncio.gather(*(limits.consume(scope, 5) for _ in range(30)), return_exceptions=True)
        assert sum(r is None for r in results) == 5
        denied = [r for r in results if isinstance(r, HTTPException)]
        assert len(denied) == 25
        assert all(r.status_code == 429 and 1 <= int(r.headers["Retry-After"]) <= 60 for r in denied)
        async with factories[0]() as db:
            before = await db.scalar(select(RequestRateWindow.attempts).where(RequestRateWindow.scope == scope))
        await asyncio.sleep(.01)
        with pytest.raises(HTTPException):
            await limits.consume(scope, 5)
        async with factories[1]() as db:
            after = await db.scalar(select(RequestRateWindow.attempts).where(RequestRateWindow.scope == scope))
        assert len(after) == 5 and after[-1] > before[-1]
        # Aging the window simulates waiting without a slow wall-clock test.
        async with factories[0]() as db, db.begin():
            row = await db.get(RequestRateWindow, scope)
            row.attempts = [stamp - 61 for stamp in row.attempts]
        await limits.consume(scope, 5)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM request_rate_windows WHERE scope=:scope"), {"scope": scope})
        await engine.dispose()
        await other_engine.dispose()


def test_worker_burst_rejections_do_not_extend_shared_cooldown():
    stamps = []
    for _ in range(5):
        stamps, wait = limits.record_attempt(stamps, 0, 30, worker=True)
        assert wait == 0
    for instant in [0, 1, 5, 9.9]:
        after, wait = limits.record_attempt(stamps, instant, 30, worker=True)
        assert after == stamps and wait == max(1, __import__("math").ceil(10-instant))
    stamps, wait = limits.record_attempt(stamps, 10, 30, worker=True)
    assert wait == 0
    # At most 30 admissions in every rolling minute, even with constant retries.
    stamps = []
    admitted = []
    for tick in range(2000):
        instant = tick / 10
        stamps, wait = limits.record_attempt(stamps, instant, 30, worker=True)
        if not wait:
            admitted.append(instant)
        assert sum(t > instant - 60 for t in admitted) <= 30
        assert sum(t > instant - 10 for t in admitted) <= 5


@pytest.mark.asyncio
async def test_retry_budget_stops_before_selection_or_dispatch(monkeypatch):
    counter = AsyncMock()
    selector = AsyncMock()
    monkeypatch.setattr(main, "count_key_attempt", counter)
    monkeypatch.setattr(main, "choose_target", selector)
    target = BackendTarget("last", "http://example.test", "/workspace", uuid4())
    with pytest.raises(HTTPException) as error:
        await main.retry_target(ApiPrincipal(uuid4(), "test"), target,
                                exclude_worker_ids={target.worker_id, uuid4(), uuid4()})
    assert error.value.status_code == 503
    assert error.value.headers["Retry-After"] == "5"
    counter.assert_not_awaited()
    selector.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["worker:", "worker-dispatch:"])
async def test_postgres_worker_burst_is_shared_and_denials_do_not_move_window(monkeypatch, prefix):
    dsn = os.environ.get("RATE_LIMIT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("requires disposable RATE_LIMIT_TEST_DATABASE_URL")
    engines = [create_async_engine(dsn), create_async_engine(dsn)]
    factories = [async_sessionmaker(e, expire_on_commit=False) for e in engines]
    scope = prefix + str(uuid4())
    async with engines[0].begin() as conn:
        await conn.run_sync(RequestRateWindow.__table__.create, checkfirst=True)
    counter = 0
    def session():
        nonlocal counter
        counter += 1
        return factories[counter % 2]()
    monkeypatch.setattr(limits, "SessionLocal", session)
    try:
        outcomes = await asyncio.gather(*(limits.consume(scope, 30) for _ in range(40)), return_exceptions=True)
        assert sum(r is None for r in outcomes) == 5
        assert sum(isinstance(r, HTTPException) and r.status_code == 429 for r in outcomes) == 35
        async with factories[0]() as db:
            before = await db.scalar(select(RequestRateWindow.attempts).where(RequestRateWindow.scope == scope))
        with pytest.raises(HTTPException):
            await limits.consume(scope, 30)
        async with factories[1]() as db:
            after = await db.scalar(select(RequestRateWindow.attempts).where(RequestRateWindow.scope == scope))
        assert before == after and len(after) == 5
    finally:
        async with engines[0].begin() as conn:
            await conn.execute(text("DELETE FROM request_rate_windows WHERE scope=:scope"), {"scope": scope})
        for e in engines:
            await e.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "claude", "gemini"])
async def test_final_gate_prevents_transport_even_after_queueing(monkeypatch, provider):
    import httpx
    from codex_gateway.backend import AppServerBackend
    from codex_gateway.claude_backend import ClaudeAdapter
    from codex_gateway.gemini_backend import GeminiAdapter
    gate = AsyncMock(side_effect=HTTPException(429, {"error": {"code": "worker_rate_limit_exceeded"}}))
    monkeypatch.setattr(limits, "admit_worker_dispatch", gate)
    transport = AsyncMock(side_effect=AssertionError("Denied request reached Worker"))
    @asynccontextmanager
    async def lease(*args):
        yield SimpleNamespace(call=transport), 0
    settings = main.get_settings()
    if provider == "codex":
        backend = AppServerBackend(settings)
        monkeypatch.setattr(backend.pool, "lease", lease)
    else:
        backend = (ClaudeAdapter if provider == "claude" else GeminiAdapter)(settings)
        monkeypatch.setattr(httpx.AsyncClient, "stream", transport)
    target = BackendTarget("key:worker", "http://example.test", "/workspace", provider=provider)
    request = ResponseRequest(model={"codex": "gpt-6-sol", "claude": "claude-test", "gemini": "gemini-test"}[provider],
                              input="hello", tool_choice="none")
    with pytest.raises(HTTPException) as error:
        async for _ in backend._turn_events(request, target):
            pass
    assert error.value.status_code == 429
    gate.assert_awaited_once_with(target)
    transport.assert_not_called()


@pytest.mark.parametrize("path,payload", [
    ("/v1/responses", {"input": "hello"}),
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hello"}]}),
    ("/v1/messages", {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 64}),
])
def test_key_cooldown_retries_never_select_worker(monkeypatch, path, payload):
    from fastapi.testclient import TestClient
    selector = AsyncMock(side_effect=AssertionError("Cooldown reached Worker selection"))
    monkeypatch.setattr(main, "choose_execution_target", selector)
    async def cooldown(key):
        raise HTTPException(429, {"error": {"type": "rate_limit_error", "code": "key_retry_cooldown"}},
                            headers={"Retry-After": "10"})
    monkeypatch.setattr(limits, "check_key_cooldown", cooldown)
    with TestClient(main.app) as client:
        for _ in range(4):
            response = client.post(path, headers={"Authorization": "Bearer cag_dev_local", "anthropic-version": "2023-06-01"},
                                   json={"model": "claude-test" if path == "/v1/messages" else "gpt-6-sol",
                                         "stream": True, **payload})
            assert response.status_code == 429
            assert response.json()["error"]["type"] == "rate_limit_error"
            if path != "/v1/messages":
                assert response.json()["error"]["code"] == "key_retry_cooldown"
            assert response.headers["Retry-After"] == "10"
    selector.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_rejection_sets_shared_key_cooldown_without_poll_extension(monkeypatch):
    dsn = os.environ.get("RATE_LIMIT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("requires disposable RATE_LIMIT_TEST_DATABASE_URL")
    engines = [create_async_engine(dsn), create_async_engine(dsn)]
    factories = [async_sessionmaker(e, expire_on_commit=False) for e in engines]
    key, worker = str(uuid4()), str(uuid4())
    scopes = [f"worker:{worker}", f"key-cooldown:{key}"]
    monkeypatch.setattr(limits, "SessionLocal", factories[0])
    token = limits._request_key.set(key)
    try:
        await limits.consume(scopes[0], 1)
        with pytest.raises(HTTPException):
            await limits.consume(scopes[0], 1)
        # Another gateway process sees the deadline committed by the first.
        monkeypatch.setattr(limits, "SessionLocal", factories[1])
        async with factories[1]() as db:
            deadline = await db.scalar(select(RequestRateWindow.attempts).where(RequestRateWindow.scope == scopes[1]))
        for _ in range(3):
            with pytest.raises(HTTPException) as error:
                await limits.check_key_cooldown(key)
            assert error.value.detail["error"]["code"] == "key_retry_cooldown"
        await limits.check_key_cooldown(str(uuid4()))  # unrelated Key unaffected
        async with factories[0]() as db, db.begin():
            row = await db.get(RequestRateWindow, scopes[1])
            assert row.attempts == deadline
            row.attempts = [deadline[0] - 61]
        await limits.check_key_cooldown(key)
    finally:
        limits._request_key.reset(token)
        async with engines[0].begin() as conn:
            for scope in scopes:
                await conn.execute(text("DELETE FROM request_rate_windows WHERE scope=:scope"), {"scope": scope})
        for engine in engines:
            await engine.dispose()
