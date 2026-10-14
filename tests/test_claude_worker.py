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
        assert exc.value.detail["code"] == "worker_execution_conflict"
    finally:
        worker.active_turns.clear()


@pytest.mark.asyncio
async def test_admission_fifo_limit_timeout_and_cancel(worker, monkeypatch):
    import asyncio
    assert worker.MAX_TURNS == 40
    assert worker.MAX_TURNS_PER_KEY == 10
    for i in range(10):
        await worker.acquire_turn(str(i), None)
    monkeypatch.setattr(worker, "MAX_WAITERS", 2)
    first = asyncio.create_task(worker.acquire_turn("first", None))
    second = asyncio.create_task(worker.acquire_turn("second", None))
    await asyncio.sleep(.01)
    with pytest.raises(HTTPException) as full:
        await worker.acquire_turn("overflow", None)
    assert full.value.detail["code"] == "worker_capacity_exceeded"
    with pytest.raises(HTTPException) as duplicate:
        await worker.acquire_turn("first", None)
    assert duplicate.value.detail["code"] == "worker_execution_conflict"
    worker.active_turns.pop("0")
    await asyncio.wait_for(first, 1)
    assert "first" in worker.active_turns and not second.done()
    assert len(worker.active_turns) == 10
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert not worker.waiting_turns
    monkeypatch.setattr(worker, "QUEUE_TIMEOUT", .02)
    with pytest.raises(HTTPException) as timeout:
        await worker.acquire_turn("timeout", None)
    assert timeout.value.detail["code"] == "worker_queue_timeout"
    assert not worker.waiting_turns and "timeout" not in worker.active_turns


@pytest.mark.asyncio
async def test_admission_disconnect_and_maintenance(worker):
    for i in range(10):
        await worker.acquire_turn(str(i), None)
    class Disconnected:
        async def is_disconnected(self):
            return True
    with pytest.raises(HTTPException) as disconnected:
        await worker.acquire_turn("gone", None, Disconnected())
    assert disconnected.value.status_code == 499
    assert not worker.waiting_turns
    worker.active_turns.clear()
    async with worker.lock:
        with pytest.raises(HTTPException) as maintenance:
            await worker.acquire_turn("new", None)
    assert maintenance.value.detail["code"] == "worker_execution_conflict"


@pytest.mark.asyncio
async def test_capacity_is_per_key_and_worker_without_head_of_line_blocking(worker):
    import asyncio
    for i in range(10):
        await worker.acquire_turn(f"a-{i}", None, key="a")
    blocked = asyncio.create_task(worker.acquire_turn("a-next", None, key="a"))
    await asyncio.sleep(.01)
    try:
        assert not blocked.done()
        # One key's queue must not strand the other 30 execution slots.
        for key in ("b", "c", "d"):
            for i in range(10):
                await asyncio.wait_for(worker.acquire_turn(f"{key}-{i}", None, key=key), .5)
        assert len(worker.active_turns) == 40
        other = asyncio.create_task(worker.acquire_turn("e-next", None, key="e"))
        await asyncio.sleep(.01)
        assert not other.done()
        worker.active_turns.pop("b-0")
        await asyncio.wait_for(other, 1)
        assert not blocked.done()
        worker.active_turns.pop("a-0")
        await asyncio.wait_for(blocked, 1)
        assert len(worker.active_turns) == 40
    finally:
        blocked.cancel()
        await asyncio.gather(blocked, return_exceptions=True)


@pytest.mark.asyncio
async def test_workspace_owns_admission_key_and_release_clears_it(worker):
    workspace = worker.ROOT / "key" / "nested"
    workspace.mkdir(parents=True)
    ticket = str(uuid4())
    body = worker.Turn(model="claude-test", session_id=ticket, workspace=str(workspace),
                       content=[{"type": "text", "text": "hi"}])
    response = await worker.turn(body)
    assert worker.turn_keys[ticket] == "key"
    response.release()
    assert ticket not in worker.turn_keys
    assert ticket not in worker.active_turns


@pytest.mark.asyncio
async def test_response_releases_once_even_if_send_fails(worker):
    async def content():
        yield "data"
    async def receive():
        return {"type": "http.disconnect"}
    async def send(message):
        raise RuntimeError("connection closed")
    worker.active_turns["ticket"] = None
    response = worker.TurnResponse(content(), "ticket")
    with pytest.raises(RuntimeError):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert "ticket" not in worker.active_turns
    await worker.acquire_turn("ticket", None)
    response.release()
    assert "ticket" in worker.active_turns


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


@pytest.mark.asyncio
async def test_bridge_progress_and_tool_result_refresh_deadlines(worker, monkeypatch):
    import asyncio
    import json
    bridge_module = sys.modules["client_bridge"]
    monkeypatch.setattr(bridge_module, "INFERENCE_IDLE_TIMEOUT", .08)
    monkeypatch.setattr(bridge_module, "INFERENCE_STAGE_TIMEOUT", .22)
    bridge = worker.ToolBridge([])
    stdout = asyncio.StreamReader()
    stream = bridge.messages(stdout)
    async def produce():
        for _ in range(6):
            await asyncio.sleep(.025)
            stdout.feed_data((json.dumps({"type": "stream_event", "event": {
                "type": "content_block_delta"}}) + "\n").encode())
    producer = asyncio.create_task(produce())
    try:
        for _ in range(6):
            assert (await anext(stream))["type"] == "stream_event"
        await producer
        old_stage = bridge.stage_started
        future = asyncio.get_running_loop().create_future()
        bridge.pending["call"] = future
        # Waiting for a client tool must not consume the inference-stage budget.
        next_event = asyncio.create_task(anext(stream))
        await asyncio.sleep(.24)
        assert not next_event.done()
        bridge.resolve("call", {})
        bridge.pending.pop("call")
        assert bridge.stage_started > old_stage
        stdout.feed_data(b'{"type":"result"}\n')
        assert (await next_event)["type"] == "result"
    finally:
        await stream.aclose()
        await producer


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["idle", "stage", "total"])
async def test_bridge_timeouts_remain_bounded(worker, monkeypatch, limit):
    import asyncio
    bridge_module = sys.modules["client_bridge"]
    monkeypatch.setattr(bridge_module, "INFERENCE_IDLE_TIMEOUT", .04 if limit == "idle" else 10)
    monkeypatch.setattr(bridge_module, "INFERENCE_STAGE_TIMEOUT", .04 if limit == "stage" else 10)
    monkeypatch.setattr(bridge_module, "EXECUTION_TIMEOUT", .04 if limit == "total" else 10)
    bridge = worker.ToolBridge([])
    if limit == "total":
        bridge.pending["call"] = asyncio.get_running_loop().create_future()
    stream = bridge.messages(asyncio.StreamReader())
    try:
        with pytest.raises(TimeoutError, match="execution timed out"):
            async with asyncio.timeout(1):
                async for _ in stream:
                    pass
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_bridge_stage_limit_survives_continuous_output(worker, monkeypatch):
    import asyncio
    bridge_module = sys.modules["client_bridge"]
    monkeypatch.setattr(bridge_module, "INFERENCE_IDLE_TIMEOUT", .1)
    monkeypatch.setattr(bridge_module, "INFERENCE_STAGE_TIMEOUT", .12)
    bridge = worker.ToolBridge([])
    stdout = asyncio.StreamReader()
    stream = bridge.messages(stdout)
    async def produce():
        while True:
            stdout.feed_data(b'{"type":"stream_event","event":{"type":"content_block_delta"}}\n')
            await asyncio.sleep(.01)
    producer = asyncio.create_task(produce())
    received = 0
    try:
        with pytest.raises(TimeoutError, match="execution timed out"):
            async with asyncio.timeout(1):
                async for event in stream:
                    if event.get("type") == "stream_event":
                        received += 1
        assert received >= 2
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
        await stream.aclose()


def test_large_tool_declarations_and_overflow(worker):
    from pydantic import ValidationError
    tools = [{"name": f"lookup_{i}", "inputSchema": {"type":"object", "properties":{}}} for i in range(512)]
    params = dict(model="claude-test", session_id=str(uuid4()), workspace=str(worker.ROOT / "key"),
                  content=[{"type":"text", "text":"hi"}])
    body = worker.Turn(**params, tools=tools)
    bridge = worker.ToolBridge(body.tools)
    assert len(bridge.tools) == 512 and "lookup_511" in bridge.tools
    overflow = tools + [{"name":"overflow"}]
    with pytest.raises(ValidationError):
        worker.Turn(**params, tools=overflow)
    with pytest.raises(ValueError):
        worker.ToolBridge(overflow)


def test_safe_result_failure_preserves_version_and_parse_diagnostics(worker):
    result = worker.result_failure({"subtype": "success", "is_error": True,
        "result": "API Error: 400 Claude Code 2.1.212 does not support this model; version 2.1.280 or newer is required."})
    assert result["code"] == "claude_client_version_unsupported"
    assert "2.1.212" in result["error"] and "2.1.280" in result["error"]
    result = worker.result_failure({"subtype": "success", "is_error": True,
        "result": "The model's tool call could not be parsed (retry also failed)."})
    assert result["code"] == "model_tool_call_invalid" and result["status"] == 502
    result = worker.result_failure({"subtype": "success", "is_error": True, "result": "private user prompt"})
    assert "private" not in result["error"] and "success" not in result["error"]


def test_turn_accepts_structured_output_with_client_tools(worker):
    body = worker.Turn(
        model="claude-test", session_id=str(uuid4()), workspace=str(worker.ROOT / "key"),
        content=[{"type": "text", "text": "Look up the answer"}],
        tools=[{"name": "lookup", "inputSchema": {"type": "object", "properties": {}}}],
        json_schema={"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
    )
    assert body.tools[0]["name"] == "lookup"
    assert body.json_schema["required"] == ["answer"]


def test_mcp_transport_and_idle_budgets_outlive_gateway_tool_deadline(worker, monkeypatch):
    import json
    import client_bridge
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "60000")
    monkeypatch.setenv("CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT", "60000")
    config = json.loads(worker.ToolBridge([]).mcp_config())["mcpServers"]["client"]
    env = worker.cli_env()
    assert config["timeout"] > client_bridge.TOOL_TTL * 1000
    assert int(env["MCP_TOOL_TIMEOUT"]) == config["timeout"]
    assert int(env["CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT"]) == config["timeout"]
