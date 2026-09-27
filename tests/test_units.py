from uuid import uuid4
from contextlib import asynccontextmanager

import pytest

import codex_gateway.main as main_module
import codex_gateway.app_server as app_server_module
from codex_gateway.auth import ApiPrincipal
from codex_gateway.app_server import AppServerCapacityError, AppServerError, AppServerPool
from codex_gateway.backend import AppServerBackend, BackendTarget, TurnUsage, WorkerFailure, _token_counts, classify_worker_failure, run_healthcheck_turn
from codex_gateway.config import get_settings
from codex_gateway.main import complete_with_failover, release_request_session
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
    request = ResponseRequest(model="gpt-6-sol", instructions="be terse", input=[{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}])
    assert request.input_text() == "be terse\n\nUSER:\nhello"


def test_empty_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        ResponseRequest(model="gpt-6-sol", input="  ")


@pytest.mark.parametrize(("payload", "expected"), [
    ({"tokenUsage": {"total": {"inputTokens": 12, "outputTokens": 3}}}, (12, 3, 0, 0)),
    ({"usage": {"input_tokens": 4, "output_tokens": 2}}, (4, 2, 0, 0)),
    ({"tokenUsage": {"total": {"inputTokens": 100, "outputTokens": 20}, "last": {"inputTokens": 12, "outputTokens": 3, "cachedInputTokens": 5, "cacheWriteInputTokens": 2}}}, (12, 3, 5, 2)),
])
def test_token_usage_variants(payload: dict, expected: tuple[int, int, int, int]) -> None:
    assert _token_counts(payload) == expected


def test_turn_usage_uses_last_then_cumulative_deltas():
    usage = TurnUsage()
    first = {"tokenUsage": {"total": {"inputTokens": 112, "outputTokens": 23, "cachedInputTokens": 55, "cacheWriteInputTokens": 12},
                            "last": {"inputTokens": 12, "outputTokens": 3, "cachedInputTokens": 5, "cacheWriteInputTokens": 2}}}
    second = {"tokenUsage": {"total": {"inputTokens": 119, "outputTokens": 27, "cachedInputTokens": 58, "cacheWriteInputTokens": 13},
                             "last": {"inputTokens": 7, "outputTokens": 4, "cachedInputTokens": 3, "cacheWriteInputTokens": 1}}}
    assert usage.observe(first) == (12, 3, 5, 2)
    assert usage.observe(first) == (12, 3, 5, 2)
    assert usage.observe(second) == (19, 7, 8, 3)


@pytest.mark.asyncio
async def test_resumed_turn_bills_only_its_own_usage():
    class FakeServer:
        async def call(self, method, _params):
            if method == 'account/read':
                return {'account': {'type': 'chatgpt'}}
            if method == 'thread/resume':
                return {'thread': {'id': 'existing-thread'}}
            if method == 'turn/start':
                return {'turn': {'id': 'new-turn'}}
            raise AssertionError(method)

        async def messages(self):
            yield {'method': 'thread/tokenUsage/updated', 'params': {'turnId': 'older-turn', 'tokenUsage': {'total': {'inputTokens': 1000, 'outputTokens': 100}}}}
            yield {'method': 'thread/tokenUsage/updated', 'params': {'turnId': 'new-turn', 'tokenUsage': {
                'total': {'inputTokens': 1200, 'outputTokens': 110, 'cachedInputTokens': 140, 'cacheWriteInputTokens': 20},
                'last': {'inputTokens': 200, 'outputTokens': 10, 'cachedInputTokens': 140, 'cacheWriteInputTokens': 20}}}}
            yield {'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}

        async def reject_server_request(self, _message):
            raise AssertionError('Unexpected server request')

    class FakePool:
        @asynccontextmanager
        async def lease(self, *_args):
            yield FakeServer(), 0

    backend = AppServerBackend(get_settings())
    backend.pool = FakePool()
    result = await backend.complete(ResponseRequest(model='gpt-6-sol', input='continue', previous_response_id='existing-thread'),
                                    BackendTarget('key:worker', 'ws://worker', '/workspace/key'))
    assert (result.input_tokens, result.output_tokens, result.cache_read_tokens, result.cache_write_tokens) == (200, 10, 140, 20)


@pytest.mark.parametrize(("message", "kind"), [
    ("Codex worker is not logged in", "logged_out"),
    ("You have reached your 5 hour usage limit", "limit"),
    ("HTTP 429 too many requests", "limit"),
    ("invalid_request_error: model is not supported", "request"),
    ("websocket closed unexpectedly", "connection"),
])
def test_worker_failure_classification(message: str, kind: str) -> None:
    assert classify_worker_failure(message) == kind


def test_generic_gpt_56_client_name_maps_to_codex_variant() -> None:
    backend = AppServerBackend(get_settings())
    assert backend.model("gpt-5.6") == "gpt-5.6-sol"
    assert backend.model("gpt-6-sol") == "gpt-6-sol"
    assert backend.model("codex") == "codex"


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
    result, selected = await complete_with_failover(ResponseRequest(model="gpt-6-sol", input="hello"), Backend(), ApiPrincipal(None, "test"), first, allow_retry=True)
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
        await complete_with_failover(ResponseRequest(model="gpt-6-sol", input="hello"), Backend(), ApiPrincipal(None, "test"), target, allow_retry=True)
    assert retry_called is False


@pytest.mark.asyncio
async def test_request_database_session_is_released_before_inference() -> None:
    calls: list[str] = []

    class FakeSession:
        def in_transaction(self):
            return True

        async def commit(self):
            calls.append("commit")

        async def close(self):
            calls.append("close")

    await release_request_session(FakeSession())
    assert calls == ["commit", "close"]


@pytest.mark.asyncio
async def test_healthcheck_turn_requires_successful_inference() -> None:
    class FakeServer:
        async def call(self, method, _params):
            return {"thread": {"id": "thread-health"}} if method == "thread/start" else {}

        async def messages(self):
            yield {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}

        async def reject_server_request(self, _message):
            return None

    await run_healthcheck_turn(FakeServer(), "gpt-test")


@pytest.mark.asyncio
async def test_healthcheck_turn_preserves_usage_limit_failure() -> None:
    class FakeServer:
        async def call(self, method, _params):
            return {"thread": {"id": "thread-health"}} if method == "thread/start" else {}

        async def messages(self):
            yield {"method": "turn/completed", "params": {"turn": {"status": "failed", "error": {"message": "You've hit your usage limit"}}}}

        async def reject_server_request(self, _message):
            return None

    with pytest.raises(WorkerFailure) as exc:
        await run_healthcheck_turn(FakeServer(), "gpt-test")
    assert exc.value.kind == "limit"


@pytest.mark.asyncio
async def test_failed_thread_resume_is_a_session_failure() -> None:
    class FakeServer:
        async def call(self, method, _params):
            assert method == "thread/resume"
            raise AppServerError("thread not found")

    backend = AppServerBackend(get_settings())
    request = ResponseRequest(model="gpt-6-sol", input="continue", previous_response_id="thread-missing")
    with pytest.raises(WorkerFailure) as exc:
        await backend._start_thread(FakeServer(), request, "/workspace/key")
    assert exc.value.kind == "session"
    assert exc.value.safe_to_retry is False


@pytest.mark.asyncio
async def test_session_failure_does_not_quarantine_or_retry(monkeypatch) -> None:
    worker_id = uuid4()
    target = BackendTarget("first", "ws://first", "/workspace/key", worker_id)
    quarantined = False
    retried = False

    class Backend:
        async def complete(self, _body, _target):
            raise WorkerFailure("thread missing", kind="session", safe_to_retry=False)

    async def fake_quarantine(*_args):
        nonlocal quarantined
        quarantined = True

    async def fake_retry(*_args):
        nonlocal retried
        retried = True

    monkeypatch.setattr(main_module, "quarantine_worker", fake_quarantine)
    monkeypatch.setattr(main_module, "retry_target", fake_retry)
    with pytest.raises(Exception) as exc:
        await complete_with_failover(ResponseRequest(model="gpt-6-sol", input="continue"), Backend(), ApiPrincipal(None, "test"), target, allow_retry=True)
    assert getattr(exc.value, "status_code", None) == 404
    assert quarantined is False
    assert retried is False


@pytest.mark.asyncio
async def test_app_server_pool_opens_parallel_slots_and_reuses_them(monkeypatch) -> None:
    created = []

    class FakeSession:
        closed = False

        async def close(self):
            self.closed = True

    async def fake_connect(*_args):
        session = FakeSession()
        created.append(session)
        return session

    monkeypatch.setattr(app_server_module, "connect_app_server", fake_connect)
    pool = AppServerPool("token", 10, max_per_group=2, max_per_worker=4, idle_ttl=600, acquire_timeout=0.1)
    async with pool.lease("key:worker", "worker", "ws://worker") as (_, first_slot):
        async with pool.lease("key:worker", "worker", "ws://worker") as (_, second_slot):
            assert {first_slot, second_slot} == {0, 1}
            assert len(created) == 2
    async with pool.lease("key:worker", "worker", "ws://worker") as (_, reused_slot):
        assert reused_slot in {0, 1}
        assert len(created) == 2
    await pool.close()


@pytest.mark.asyncio
async def test_app_server_pool_returns_capacity_error_after_timeout(monkeypatch) -> None:
    class FakeSession:
        closed = False

        async def close(self):
            self.closed = True

    async def fake_connect(*_args):
        return FakeSession()

    monkeypatch.setattr(app_server_module, "connect_app_server", fake_connect)
    pool = AppServerPool("token", 10, max_per_group=1, max_per_worker=1, idle_ttl=600, acquire_timeout=0.01)
    async with pool.lease("key:worker", "worker", "ws://worker"):
        with pytest.raises(AppServerCapacityError):
            async with pool.lease("key:worker", "worker", "ws://worker"):
                pass
    await pool.close()


@pytest.mark.asyncio
async def test_logout_rpc_serializes_null_params():
    import json
    from codex_gateway.app_server import AppServerSession
    sent=[]
    class Socket:
        async def send(self,body):sent.append(json.loads(body))
    session=AppServerSession(Socket(),10)
    await session.send('account/logout')
    await session.send('account/login/start',{'type':'chatgptDeviceCode'})
    assert sent[0]['params'] is None
    assert sent[1]['params']=={'type':'chatgptDeviceCode'}

@pytest.mark.asyncio
async def test_cache_affinity_preserves_explicit_routing_and_exclusions():
    from types import SimpleNamespace
    workers=[SimpleNamespace(id=uuid4(),enabled=True,status='ready',container_name=f'w{i}',endpoint=f'ws://w{i}',execution_generation=0) for i in range(3)]
    class Session:
        async def scalars(self,*args):return SimpleNamespace(all=lambda:list(workers))
        async def get(self,model,key):return next(w for w in workers if w.id==key)
    db=Session();principal=ApiPrincipal(uuid4(),'test')
    first=await main_module.choose_target(principal,db,cache_affinity='shared-prefix')
    workers.reverse()
    assert (await main_module.choose_target(principal,db,cache_affinity='shared-prefix')).worker_id==first.worker_id
    other=next(w for w in workers if w.id!=first.worker_id)
    assert (await main_module.choose_target(principal,db,SimpleNamespace(worker_id=other.id),cache_affinity='shared-prefix')).worker_id==other.id
    assert (await main_module.choose_target(ApiPrincipal(principal.key_id,'test',pinned_worker_id=other.id),db,cache_affinity='shared-prefix')).worker_id==other.id
    assert (await main_module.choose_target(principal,db,exclude_worker_ids={first.worker_id},cache_affinity='shared-prefix')).worker_id!=first.worker_id
