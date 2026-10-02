"""Exercise a Claude worker's private HTTP protocol end to end.

Usage: WORKER_URL=http://127.0.0.1:4501 CODEX_WORKER_TOKEN=... python scripts/validate_claude_worker.py
Runs real inference on the worker's subscription; returns synthetic tool results and executes no model output.
"""
import base64
import json
import os
import struct
import sys
import time
import zlib
from uuid import uuid4

import httpx

URL = os.environ.get("WORKER_URL", "http://127.0.0.1:4501")
HEADERS = {"Authorization": "Bearer " + os.environ["CODEX_WORKER_TOKEN"]}
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
WORKSPACE = os.environ.get("WORKER_WORKSPACE", "/workspace/validation")


def rpc(path, payload=None, timeout=120):
    response = httpx.post(URL + path, json=payload or {}, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.json()


def png():
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\x00\x00\xff" * 16 for _ in range(16))
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return base64.b64encode(data).decode()


def turn(payload, on_tool=None):
    """Stream one worker turn; answer client tool calls through on_tool."""
    text, events, terminal = [], [], None
    with httpx.stream("POST", URL + "/turn", json=payload, headers=HEADERS, timeout=300) as response:
        assert response.status_code == 200, (response.status_code, response.read())
        for line in response.iter_lines():
            if not line:
                continue
            data = json.loads(line)
            events.append(data)
            if data.get("heartbeat"):
                continue
            if data.get("error"):
                raise RuntimeError(data)
            if data.get("event") == "client_tool":
                assert on_tool, "unexpected tool call"
                reply = on_tool(data)
                rpc("/tool-result", {"run_id": data["run_id"], "worker_call_id": data["worker_call_id"], "content": reply})
                continue
            if data.get("delta"):
                text.append(data["delta"])
            if data.get("done"):
                terminal = data
    assert terminal, "stream ended without terminal"
    return "".join(text), terminal, events


def main():
    print("capabilities:", rpc("/capabilities"))
    account = rpc("/account")
    print("account:", account)
    assert account.get("account"), "worker is not logged in"
    started = time.monotonic()
    probe = rpc("/probe", {"model": MODEL}, timeout=180)
    print("probe (%.1fs):" % (time.monotonic() - started), json.dumps(probe)[:300])
    assert probe.get("available") is not False, probe
    print("rate-limits:", rpc("/rate-limits", {"model": MODEL}, timeout=180))

    session = str(uuid4())
    text, terminal, events = turn({"model": MODEL, "session_id": session, "workspace": WORKSPACE,
                                   "system": "You are a terse test responder.",
                                   "content": [{"type": "text", "text": "Reply with exactly: PROBE-ONE"}]})
    kinds = [e.get("stream", {}).get("type") for e in events if e.get("stream")]
    print("text turn:", repr(text), terminal["stop_reason"], {k: terminal[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")})
    assert "PROBE-ONE" in text and terminal["thread_id"] == session, (text, terminal)
    assert "message_start" in kinds and "message_stop" in kinds, kinds

    text, terminal, _ = turn({"model": MODEL, "session_id": str(uuid4()), "conversation": session, "workspace": WORKSPACE,
                              "content": [{"type": "text", "text": "What exact string did I ask you to reply with? Only the string."}]})
    print("resume turn:", repr(text), terminal["thread_id"])
    assert "PROBE-ONE" in text and terminal["thread_id"] == session

    calls = []
    def on_tool(data):
        calls.append(data)
        assert data["tool"] == "get_weather" and data["arguments"].get("city"), data
        return [{"type": "text", "text": "Sunny, 23C in " + data["arguments"]["city"]}]
    text, terminal, _ = turn({"model": MODEL, "session_id": str(uuid4()), "workspace": WORKSPACE,
                              "tools": [{"name": "get_weather", "description": "Client-side tool get_weather. Returns the weather for a city.",
                                         "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}],
                              "content": [{"type": "text", "text": "Use the get_weather client tool for Paris and report the result in one sentence."}]},
                             on_tool)
    print("tool turn:", repr(text), "calls:", [(c["tool"], c["arguments"], c.get("tool_use_id")) for c in calls])
    assert len(calls) == 1 and "23" in text

    text, terminal, _ = turn({"model": MODEL, "session_id": str(uuid4()), "workspace": WORKSPACE,
                              "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png()}},
                                          {"type": "text", "text": "What single color fills this image? One word."}]})
    print("image turn:", repr(text))
    assert "blue" in text.lower(), text

    text, terminal, _ = turn({"model": MODEL, "session_id": str(uuid4()), "workspace": WORKSPACE,
                              "json_schema": {"type": "object", "properties": {"city": {"type": "string"}, "population": {"type": "integer"}},
                                              "required": ["city", "population"], "additionalProperties": False},
                              "content": [{"type": "text", "text": "Give the capital of France and an estimated population."}]})
    parsed = json.loads(text)
    print("schema turn:", parsed)
    assert parsed["city"].lower().startswith("paris") and isinstance(parsed["population"], int)

    response = httpx.post(URL + "/turn", json={"model": MODEL, "session_id": "not-a-uuid", "workspace": WORKSPACE, "content": [{"type": "text", "text": "x"}]}, headers=HEADERS, timeout=30)
    print("invalid session id ->", response.status_code)
    assert response.status_code == 422
    response = httpx.post(URL + "/turn", json={}, headers={}, timeout=30)
    assert response.status_code == 401, response.status_code
    print("ALL PASSED")


if __name__ == "__main__":
    sys.exit(main())
