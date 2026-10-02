"""Real Claude gateway smoke tests; uses an existing persisted gateway API key.

GATEWAY_URL=http://... GATEWAY_API_KEY=... python scripts/validate_claude_tools.py
Only synthetic client tool results are returned; no model-generated code executes.
"""
import asyncio
import json
import os
from uuid import uuid4

import httpx
import validate_gemini_tools as openai_checks

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
openai_checks.MODEL = MODEL


async def native(client, body):
    response = await client.post("/v1/messages", headers={"anthropic-version": "2023-06-01"}, json={"model": MODEL, "max_tokens": 1024, **body})
    assert response.status_code == 200, (response.status_code, response.text[:500])
    assert response.headers["x-gateway-generation-policy"] == "worker-defaults"
    if not body.get("stream"):
        return response.json()
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[-1]["type"] == "message_stop" and not any(e["type"] == "error" for e in events), events
    message = events[0]["message"]
    blocks = {}
    for event in events[1:]:
        if event["type"] == "content_block_start":
            blocks[event["index"]] = event["content_block"]
        elif event["type"] == "content_block_delta":
            block, delta = blocks[event["index"]], event["delta"]
            if delta["type"] == "text_delta":
                block["text"] += delta["text"]
            else:
                block["_json"] = block.get("_json", "") + delta["partial_json"]
        elif event["type"] == "message_delta":
            message.update(event["delta"])
            message["usage"] = event["usage"]
    for block in blocks.values():
        if "_json" in block:
            block["input"] = json.loads(block.pop("_json"))
    message["content"] = list(blocks.values())
    return message


async def verify_native(client):
    for streaming in (False, True):
        marker = "NATIVE_" + uuid4().hex
        messages = [{"role": "user", "content": "Call lookup once with query weather, then report exactly the returned marker."}]
        tools = [{"name": "lookup", "description": "Read the verification marker from the client.",
                  "input_schema": openai_checks.TOOL["parameters"]}]
        first = await native(client, {"messages": messages, "tools": tools, "stream": streaming})
        calls = [p for p in first["content"] if p["type"] == "tool_use"]
        assert len(calls) == 1 and first["stop_reason"] == "tool_use"
        messages += [{"role": "assistant", "content": first["content"]}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": calls[0]["id"], "content": marker}]}]
        final = await native(client, {"messages": messages, "tools": tools, "stream": streaming})
        assert final["stop_reason"] == "end_turn" and marker in json.dumps(final["content"])
        print(f"PASS native tools stream={streaming}", flush=True)
    body = {"messages": [{"role": "user", "content": "Return an object with ok=true."}],
            "output_config": {"format": {"type": "json_schema", "schema": {"type": "object",
              "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}}}}
    final = await native(client, body)
    assert json.loads(final["content"][0]["text"]) == {"ok": True}
    print("PASS native JSON Schema", flush=True)
    response = await client.post("/v1/messages/count_tokens", headers={"anthropic-version": "2023-06-01"},
                                 json={"model": MODEL, "messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 200 and response.headers["x-gateway-token-count"] == "estimated"
    print("PASS authenticated estimated token count", flush=True)


async def main():
    async with httpx.AsyncClient(base_url=os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000"), timeout=360,
            headers={"Authorization": "Bearer " + os.environ["GATEWAY_API_KEY"]}) as client:
        await openai_checks.verify(client)
        await verify_native(client)


if __name__ == "__main__":
    asyncio.run(main())
