"""Real Gemini client-tool smoke test. Requires an existing persisted API key.

GATEWAY_URL=http://... GATEWAY_API_KEY=... python scripts/validate_gemini_tools.py
The client returns synthetic tool results; it never executes model-generated code.
"""
import asyncio
import json
import os
from uuid import uuid4

import httpx

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash-high")
TOOL = {"type": "function", "name": "lookup", "description": "Get the verification marker from the client.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}


async def request(client, path, body):
    response = await client.post(path, json=body)
    assert response.status_code == 200, (response.status_code, response.text[:500])
    if not body.get("stream"):
        return response.json()
    events = [json.loads(line[6:]) for line in response.text.splitlines()
              if line.startswith("data: ") and line[6:] != "[DONE]"]
    assert events and all(event.get("type") not in {"error", "response.failed"} and "error" not in event for event in events), "Stream failed"
    if path == "/v1/responses":
        completed = [event["response"] for event in events if event.get("type") == "response.completed"]
        assert len(completed) == 1, "Missing Responses completion"
        return completed[0]
    calls, text = {}, ""
    finish = None
    for event in events:
        for choice in event.get("choices", []):
            delta = choice.get("delta", {})
            text += delta.get("content") or ""
            finish = choice.get("finish_reason") or finish
            for call in delta.get("tool_calls", []):
                item = calls.setdefault(call["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                item["id"] += call.get("id", "")
                for key in ("name", "arguments"):
                    item["function"][key] += call.get("function", {}).get(key, "")
    return {"choices": [{"message": {"role": "assistant", "content": text, "tool_calls": list(calls.values())},
                         "finish_reason": finish}]}


async def verify(client, surfaces=("responses", "chat")):
    for streaming in ((False, True) if "responses" in surfaces else ()):
        marker = "CLIENT_RESULT_" + uuid4().hex
        # Streamed Responses also covers namespaced, grammar-constrained custom input.
        tools = ([{"type": "namespace", "name": "functions", "tools": [{
            "type": "custom", "name": "lookup", "description": "Get the verification marker. Input must be weather.",
            "format": {"type": "grammar", "syntax": "lark", "definition": 'start: "weather"'}}]}]
                 if streaming else [TOOL])
        first = await request(client, "/v1/responses", {"model": MODEL, "stream": streaming,
            "input": "Call the client lookup tool once with weather as query/input. Then report exactly the marker returned by it.",
            "tools": tools})
        calls = [item for item in first["output"] if item["type"] in ("function_call", "custom_tool_call")]
        assert len(calls) == 1, "Expected one client tool call"
        call = calls[0]
        assert call["name"] == "lookup"
        if streaming:
            assert call["namespace"] == "functions" and call["input"] == "weather"
        result = {"type": "custom_tool_call_output" if streaming else "function_call_output",
                  "call_id": call["call_id"], "output": marker}
        followup = {"model": MODEL, "stream": streaming, "previous_response_id": first["id"], "input": [result]}
        final = await request(client, "/v1/responses", followup)
        assert marker in json.dumps(final["output"]), "Client result was not consumed"
        assert not any(item["type"].endswith("_call") for item in final["output"])
        replay = await client.post("/v1/responses", json={**followup, "stream": False})
        assert replay.status_code == 400, "Duplicate result was accepted"
        print(f"PASS Responses tools stream={streaming}, previous_response_id, replay rejection", flush=True)

    for streaming in ((False, True) if "chat" in surfaces else ()):
        marker = "CLIENT_RESULT_" + uuid4().hex
        messages = [{"role": "user", "content": "Call lookup once with query weather, then report exactly the returned marker."}]
        tools = [{"type": "function", "function": {k: v for k, v in TOOL.items() if k != "type"}}]
        first = await request(client, "/v1/chat/completions", {"model": MODEL, "stream": streaming, "messages": messages, "tools": tools})
        assert first["choices"][0]["finish_reason"] == "tool_calls"
        final = first
        call_ids = set()
        for _ in range(3):
            assistant = final["choices"][0]["message"]
            assert len(assistant["tool_calls"]) == 1
            call = assistant["tool_calls"][0]
            assert call["id"] not in call_ids, "Reused a completed call ID"
            call_ids.add(call["id"])
            assert call["function"]["name"] == "lookup"
            messages.extend([assistant, {"role": "tool", "tool_call_id": call["id"], "content": marker}])
            final = await request(client, "/v1/chat/completions", {"model": MODEL, "stream": streaming, "messages": messages, "tools": tools})
            if final["choices"][0]["finish_reason"] != "tool_calls":
                break
        assert marker in final["choices"][0]["message"]["content"], final["choices"]
        assert final["choices"][0]["finish_reason"] == "stop"
        print(f"PASS Chat tools stream={streaming}, calls={len(call_ids)}", flush=True)


async def main():
    async with httpx.AsyncClient(base_url=os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000"), timeout=360,
                                 headers={"Authorization": "Bearer " + os.environ["GATEWAY_API_KEY"]}) as client:
        await verify(client)


if __name__ == "__main__":
    asyncio.run(main())
