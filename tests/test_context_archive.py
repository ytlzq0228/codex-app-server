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
