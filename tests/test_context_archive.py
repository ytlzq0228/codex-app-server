import importlib.util
import json
from datetime import datetime, timezone
from uuid import uuid4
import pytest

spec = importlib.util.spec_from_file_location("archive_context", "scripts/archive_request_context.py")
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)

def test_projection_keeps_request_features():
    value = {"input": [{"content": "secret"}], "messages": [], "instructions": "system",
             "model": "model", "tools": [{"name":"tool"}], "metadata": {"user_id":"owner"},
             "stream": True, "temperature": 0.5, "previous_response_id": "resp"}
    result, fields = archive.projection(value, {"path":"archive"})
    assert fields == ["input", "instructions", "messages"]
    assert "input" in value
    assert {k:v for k,v in result.items() if k != archive.MARKER} == {
        k:v for k,v in value.items() if k not in archive.CONTEXT_KEYS}
    assert result[archive.MARKER]["path"] == "archive"

def test_archive_lossless_and_retry(tmp_path):
    raw = '{"messages": [{"content": "\\u0000 中文"}], "model": "test"}'
    ident = str(uuid4())
    at = datetime.now(timezone.utc)
    ref = archive.save_verified(tmp_path, ident, "request", raw, at)
    assert archive.read_verified(tmp_path, ref["path"], ref["sha256"], ident) == raw
    assert archive.save_verified(tmp_path, ident, "request", raw, at) == ref
    assert len(list(tmp_path.rglob("*.gz"))) == 1
    assert not list(tmp_path.rglob(".pending-*"))

def test_corruption_never_overwritten(tmp_path):
    ident = str(uuid4())
    at = datetime.now(timezone.utc)
    ref = archive.save_verified(tmp_path, ident, "request", "{}", at)
    (tmp_path/ref["path"]).write_bytes(b"damaged")
    with pytest.raises(Exception):
        archive.save_verified(tmp_path, ident, "request", "{}", at)

def test_identity_and_path_guard(tmp_path):
    ident = str(uuid4())
    ref = archive.save_verified(tmp_path, ident, "request", "{}", datetime.now(timezone.utc))
    with pytest.raises(ValueError):
        archive.read_verified(tmp_path, ref["path"], ref["sha256"], str(uuid4()))
    with pytest.raises(ValueError):
        archive.read_verified(tmp_path, "../outside", ref["sha256"], ident)

def test_no_context_and_reserved_marker():
    assert archive.projection({"model":"x"}, {}) == ({"model":"x"}, [])
    assert archive.projection(None, {}) == (None, [])
    with pytest.raises(ValueError):
        archive.projection({archive.MARKER:{}, "input":"secret"}, {})

def test_write_failure_preserves_source(tmp_path, monkeypatch):
    params = {"input":"secret"}
    monkeypatch.setattr(archive.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        archive.save_verified(tmp_path, str(uuid4()), "request", json.dumps(params), datetime.now(timezone.utc))
    assert params == {"input":"secret"}
    assert not list(tmp_path.rglob("*.gz"))


def test_retry_classification_follows_masked_disconnect():
    import asyncpg
    disconnect = asyncpg.ConnectionDoesNotExistError("disconnected")
    masked = asyncpg.InterfaceError("transaction cleanup failed")
    masked.__context__ = disconnect
    assert archive.retryable_database_error(masked)
    assert archive.retryable_database_error(asyncpg.AdminShutdownError("restart"))
    assert archive.retryable_database_error(ConnectionResetError())
    assert archive.retryable_database_error(TimeoutError())
    assert not archive.retryable_database_error(asyncpg.InterfaceError("programming error"))
    assert not archive.retryable_database_error(OSError("disk full"))
    assert not archive.retryable_database_error(ValueError("checksum mismatch"))
    assert not archive.retryable_database_error(asyncpg.InvalidPasswordError("bad password"))


@pytest.mark.asyncio
async def test_retry_preserves_cutoff_and_progress(monkeypatch):
    from types import SimpleNamespace
    states, delays = [], []
    cutoff = datetime.now(timezone.utc)

    async def attempt(args, state):
        states.append(state.copy())
        if len(states) == 1:
            state.update(cutoff=cutoff, completed=3)
            raise ConnectionResetError()
        assert state["cutoff"] == cutoff and state["completed"] == 3

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(archive, "run_attempt", attempt)
    monkeypatch.setattr(archive.asyncio, "sleep", sleep)
    await archive.run(SimpleNamespace(max_retries=8, retry_delay=5))
    assert len(states) == 2 and delays == [5]


@pytest.mark.asyncio
async def test_retry_has_bounded_backoff(monkeypatch):
    from types import SimpleNamespace
    delays = []

    async def attempt(args, state):
        raise ConnectionResetError()

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(archive, "run_attempt", attempt)
    monkeypatch.setattr(archive.asyncio, "sleep", sleep)
    with pytest.raises(ConnectionResetError):
        await archive.run(SimpleNamespace(max_retries=8, retry_delay=5))
    assert delays == [5, 10, 20, 40, 60, 60, 60, 60]


@pytest.mark.asyncio
async def test_permanent_error_is_not_retried(monkeypatch):
    from types import SimpleNamespace
    calls = []

    async def attempt(args, state):
        calls.append(1)
        raise ValueError("checksum mismatch")

    monkeypatch.setattr(archive, "run_attempt", attempt)
    with pytest.raises(ValueError):
        await archive.run(SimpleNamespace(max_retries=8, retry_delay=5))
    assert calls == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["before_update", "after_commit"])
async def test_postgres_disconnect_resume(tmp_path, monkeypatch, failure_point):
    """Opt-in isolated-schema test on a disposable PostgreSQL instance."""
    import os
    from types import SimpleNamespace
    import asyncpg
    dsn = os.environ.get("ARCHIVE_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("requires disposable ARCHIVE_TEST_DATABASE_URL")
    connect = asyncpg.connect
    control = await connect(dsn)
    schema = "archive_test_" + uuid4().hex
    await control.execute(f'CREATE SCHEMA "{schema}"')
    await control.execute(f'SET search_path TO "{schema}"')
    disconnected = False
    connections = []
    lock_attempts = []

    class Transaction:
        def __init__(self, connection):
            self.connection = connection
            self.inner = connection.transaction()

        async def __aenter__(self):
            return await self.inner.__aenter__()

        async def __aexit__(self, *exc):
            nonlocal disconnected
            result = await self.inner.__aexit__(*exc)
            if failure_point == "after_commit" and not disconnected and exc[0] is None:
                disconnected = True
                self.connection.terminate()
                raise ConnectionResetError("lost commit acknowledgement")
            return result

    class Connection:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def transaction(self):
            return Transaction(self.inner)

        async def fetchval(self, query, *args):
            if "pg_try_advisory_lock" in query:
                lock_attempts.append(self.inner.get_server_pid())
            return await self.inner.fetchval(query, *args)

        async def execute(self, query, *args):
            nonlocal disconnected
            if failure_point == "before_update" and not disconnected and query.startswith("UPDATE"):
                disconnected = True
                await control.execute("SELECT pg_terminate_backend($1)", self.inner.get_server_pid())
                # Let asyncpg observe the terminated connection, as on proxy restart.
                await archive.asyncio.sleep(0.05)
            return await self.inner.execute(query, *args)

    async def archive_connect(url, **kwargs):
        kwargs["server_settings"]["search_path"] = schema
        inner = await connect(url, **kwargs)
        connections.append(inner)
        return Connection(inner)

    try:
        await control.execute("""
CREATE TABLE usage_records (
 id uuid PRIMARY KEY, request_id varchar(80), created_at timestamptz,
 request_params json, billing_tokens bigint)
""")
        await control.execute(archive.DDL)
        raw = json.dumps({"messages": [{"content": "test context"}], "model": "test",
                          "metadata": {"user_id": "owner"}})
        for age in (20, 19, 1):
            await control.execute(
                "INSERT INTO usage_records VALUES($1,$2,now()-$3*interval '1 day',$4::json,123)",
                uuid4(), str(age), age, raw)
        monkeypatch.setenv("CODEX_GATEWAY_DATABASE_URL", dsn)
        monkeypatch.setattr(asyncpg, "connect", archive_connect)
        args = SimpleNamespace(root=tmp_path, read=None, initialize=False, dry_run=False,
                               max_records=100, min_free_gib=0, max_retries=2, retry_delay=0)
        await archive.run(args)
        assert disconnected and len(connections) == 2
        assert len(set(lock_attempts)) == 2
        rows = await control.fetch("""
SELECT u.*,a.path,a.sha256 FROM usage_records u
LEFT JOIN request_context_archives a ON a.usage_id=u.id ORDER BY u.created_at
""")
        assert len(rows) == 3
        for row in rows[:2]:
            params = json.loads(row["request_params"])
            assert row["billing_tokens"] == 123
            assert params["metadata"] == {"user_id": "owner"}
            assert params["model"] == "test" and "messages" not in params
            assert archive.read_verified(tmp_path, row["path"], row["sha256"], str(row["id"])) == raw
        assert rows[2]["request_params"] == raw and rows[2]["path"] is None
        assert await control.fetchval("SELECT count(*) FROM request_context_archives") == 2
        assert len(list(tmp_path.rglob("*.gz"))) == 2
        assert all(c.is_closed() for c in connections)
    finally:
        await control.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await control.close()
