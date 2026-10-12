"""Verify large tool catalogs with real calls and synthetic client results.
Run with GATEWAY_URL and GATEWAY_API_KEY. Never executes model-generated code.
"""
import asyncio
import json
import os
import time
from uuid import uuid4
import httpx


def tools(count):
    return [{"type": "function", "name": f"catalog_probe_{i:03d}",
             "description": f"Retrieve the verification value for catalog entry {i}.",
             "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}
            for i in range(count)]


async def check(client, model, count, endpoint="responses"):
    catalog = tools(count)
    target = catalog[-1]["name"]
    prompt = f"Call {target} exactly once with empty arguments. Then reply with only the value returned by that tool. Do not call any other tool."
    marker = "VERIFIED_" + uuid4().hex
    started = time.monotonic()
    if endpoint == "responses":
        body = {"model": model, "input": prompt, "tools": catalog}
    elif endpoint == "chat/completions":
        body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                "tools": [{"type": "function", "function": {k:v for k,v in t.items() if k != "type"}} for t in catalog]}
    else:
        body = {"model": model, "max_tokens": 1024, "messages": [{"role": "user", "content": prompt}],
                "tools": [{"name":t["name"], "description":t["description"], "input_schema":t["parameters"]} for t in catalog]}
    r = await client.post("/v1/" + endpoint, json=body)
    assert r.status_code == 200, (r.status_code, r.text[:600])
    first = r.json()
    if endpoint == "responses":
        calls = [i for i in first["output"] if i["type"] == "function_call"]
        assert len(calls) == 1 and calls[0]["name"] == target, first
        assert json.loads(calls[0]["arguments"]) == {}
        body["previous_response_id"] = first["id"]
        body["input"] = [{"type": "function_call_output", "call_id": calls[0]["call_id"], "output": marker}]
    elif endpoint == "chat/completions":
        message = first["choices"][0]["message"]
        calls = message.get("tool_calls", [])
        assert len(calls) == 1 and calls[0]["function"]["name"] == target, first
        body["messages"] += [message, {"role": "tool", "tool_call_id": calls[0]["id"], "content": marker}]
    else:
        calls = [i for i in first["content"] if i["type"] == "tool_use"]
        assert len(calls) == 1 and calls[0]["name"] == target, first
        body["messages"] += [{"role":"assistant", "content": first["content"]},
                            {"role":"user", "content":[{"type":"tool_result", "tool_use_id":calls[0]["id"], "content":marker}]}]
    r = await client.post("/v1/" + endpoint, json=body)
    assert r.status_code == 200, (r.status_code, r.text[:600])
    final = r.json()
    assert marker in json.dumps(final), final
    if endpoint == "responses":
        assert final["status"] == "completed" and not any(i["type"] == "function_call" for i in final["output"]), final
    elif endpoint == "chat/completions":
        assert final["choices"][0]["finish_reason"] == "stop", final
    else:
        assert final["stop_reason"] == "end_turn", final
    result = {"model":model, "tools":count, "endpoint":endpoint, "status":"passed",
              "called":target, "seconds":round(time.monotonic()-started,2)}
    print(json.dumps(result), flush=True)
    return result


async def verify(key, cases):
    async with httpx.AsyncClient(base_url=os.environ.get("GATEWAY_URL","http://127.0.0.1:8000"),
            headers={"Authorization":"Bearer "+key, "anthropic-version":"2023-06-01"}, timeout=240) as client:
        for model, count, endpoint in cases:
            try:
                await check(client, model, count, endpoint)
            except Exception as exc:
                print(json.dumps({"model":model,"tools":count,"endpoint":endpoint,"status":"failed","error":str(exc)[:1000]}), flush=True)
                raise
        r = await client.post("/v1/responses", json={"model":cases[0][0],"input":"Reply OK","tools":tools(513)})
        assert r.status_code == 400 and "At most 512" in r.text, (r.status_code,r.text[:500])
        print(json.dumps({"tools":513,"status":"correctly_rejected"}), flush=True)


if __name__ == "__main__":
    model = os.environ.get("TEST_MODEL","claude-sonnet-5-5")
    cases = [(model,n,"responses") for n in (65,128,256,321,512)]
    cases += [(model,512,endpoint) for endpoint in ("chat/completions","messages")]
    asyncio.run(verify(os.environ["GATEWAY_API_KEY"], cases))
