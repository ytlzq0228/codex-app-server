import importlib.util
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

import pytest
from fastapi import HTTPException


@pytest.fixture
def worker(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1] / "worker" / "claude"
    def load(name, file):
        spec = importlib.util.spec_from_file_location(name, root / file)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module
    load("client_bridge", "client_bridge.py")
    service = load("claude_worker_test", "service.py")
    monkeypatch.setattr(service, "ROOT", tmp_path / "workspace")
    service.ROOT.mkdir()
    monkeypatch.setattr(service, "SESSION_ROOT", tmp_path / "sessions")
    service.SESSION_ROOT.mkdir()
    return service


@pytest.mark.asyncio
async def test_same_session_rejected_before_subprocess_start(worker):
    workspace = worker.ROOT / "key"
    workspace.mkdir()
    body = worker.Turn(model="claude-test", session_id=str(uuid4()), workspace=str(workspace),
                       content=[{"type": "text", "text": "hi"}])
    await worker.turn(body)
    try:
        with pytest.raises(HTTPException) as exc:
            await worker.turn(body)
        assert exc.value.status_code == 409
        assert "Conversation" in exc.value.detail
    finally:
        worker.active_turns.clear()


@pytest.mark.asyncio
async def test_prune_preserves_bound_active_recent_and_foreign_files(worker):
    folder = worker.SESSION_ROOT / "project"
    folder.mkdir()
    ids = [str(uuid4()) for _ in range(4)]
    for ident in ids:
        path = folder / (ident + ".jsonl")
        path.write_text("{}")
        os.utime(path, (time.time() - 172800,) * 2)
    (folder / (ids[3] + ".jsonl")).touch()
    (folder / "other.jsonl").write_text("{}")
    worker.active_turns[ids[1]] = None
    result = await worker.prune_sessions(worker.PruneInput(keep_ids=[ids[0]]))
    assert result["removed"] == 1
    assert not (folder / (ids[2] + ".jsonl")).exists()
    assert all((folder / (ids[i] + ".jsonl")).exists() for i in (0, 1, 3))
    assert (folder / "other.jsonl").exists()


@pytest.mark.asyncio
async def test_logout_clears_transcripts_and_cached_usage(worker, monkeypatch):
    (worker.SESSION_ROOT / "old.jsonl").write_text("{}")
    worker.rate_limit.update(info={"status": "allowed"}, at=time.time())
    async def run(*args, **kwargs):
        return 0, "", ""
    async def read():
        return None, "logged_out"
    monkeypatch.setattr(worker, "run_json", run)
    monkeypatch.setattr(worker, "read_account", read)
    assert not (await worker.logout())["logged_in"]
    assert not worker.SESSION_ROOT.exists()
    assert worker.rate_limit["info"] is None


def test_usage_includes_both_cache_classes(worker):
    assert worker.usage_counts({"input_tokens": 2, "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 8, "output_tokens": 3}) == {
        "input_tokens": 15, "cache_read_tokens": 5, "cache_write_tokens": 8, "output_tokens": 3}


@pytest.mark.asyncio
@pytest.mark.parametrize('output', [b'Login successful\r\n', b''])
async def test_login_exit_success_drains_pty_and_confirms_account(worker, monkeypatch, output):
    from types import SimpleNamespace
    import asyncio
    session = worker.Login()
    session.code_submitted = True
    session.fd = 123
    session.process = SimpleNamespace(returncode=0)
    chunks = iter([output])
    def read(fd, size):
        return next(chunks, b'')
    async def account():
        return {'type':'claude-subscription', 'email':'test@example.test'}, None
    async def stop(process):
        pass
    monkeypatch.setattr(worker.os, 'read', read)
    monkeypatch.setattr(worker.os, 'close', lambda fd: None)
    monkeypatch.setattr(worker, 'read_account', account)
    monkeypatch.setattr(worker, 'stop', stop)
    session.task = asyncio.create_task(session.read())
    await session.task
    assert session.error is None
    assert session.state()['logged_in'] is True
    assert session.state()['expires_in'] > 500


@pytest.mark.asyncio
@pytest.mark.parametrize('code_submitted,exit_code', [(False,0),(True,1)])
async def test_login_failed_exit_does_not_accept_previous_account(worker,monkeypatch,code_submitted,exit_code):
    from types import SimpleNamespace
    session=worker.Login()
    session.code_submitted=code_submitted
    session.fd=123
    session.process=SimpleNamespace(returncode=exit_code)
    async def account():
        raise AssertionError('must not accept previous credentials')
    async def stop(process):
        pass
    monkeypatch.setattr(worker.os,'read',lambda *args:b'')
    monkeypatch.setattr(worker.os,'close',lambda fd:None)
    monkeypatch.setattr(worker,'read_account',account)
    monkeypatch.setattr(worker,'stop',stop)
    await session.read()
    assert not session.account
    assert session.error


@pytest.mark.asyncio
async def test_login_reads_real_pty_after_fast_child_exit(worker,monkeypatch):
    import asyncio
    import pty
    session=worker.Login()
    master,slave=pty.openpty()
    session.fd=master
    os.set_blocking(master,False)
    session.code_submitted=True
    session.process=await asyncio.create_subprocess_exec('/bin/sh','-c',"printf 'Login successful\\n'",stdout=slave,stderr=slave)
    os.close(slave)
    await session.process.wait()
    async def account():
        return {'type':'claude-subscription','email':'test@example.test'},None
    monkeypatch.setattr(worker,'read_account',account)
    session.task=asyncio.create_task(session.read())
    await session.task
    assert 'Login successful' in session.text
    assert session.state()['logged_in']
    assert not session.error
