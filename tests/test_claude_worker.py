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
