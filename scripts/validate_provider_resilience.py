"""Real-provider recovery checks against a dedicated test gateway.

Set GATEWAY_URL and GATEWAY_API_KEY. No model-generated commands execute: lookup
returns synthetic strings. Use --phase seed before a gateway restart, then
--phase verify with the same --state path to exercise lost suspended executions.
Credentials are never saved in the state/report.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
from validate_gemini_tools import request

MODELS = ("claude-sonnet-5-5", "gemini-3.8-flash-medium")
TOOLS = [
    {"type": "function", "name": "lookup", "description": "Read a synthetic test marker.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"type": "function", "name": "unused", "description": "Unused test tool.",
     "parameters": {"type": "object", "properties": {}}},
]


class ScopedClient:
    def __init__(self, client, session, headers=None):
        self.client, self.session = client, session
        self.headers = headers or {}

    async def post(self, path, **kwargs):
        return await self.client.post(path, headers={"thread-id": self.session, **self.headers}, **kwargs)


def calls(result):
    return [i for i in result["output"] if i["type"] in {"function_call", "custom_tool_call"}]


async def start(client, model, stream=False):
    session = str(uuid4())
    client = ScopedClient(client, session)
    body = {"model": model, "stream": stream, "tools": TOOLS,
            "input": [{"role": "user", "content":
                "Call lookup exactly once with query=marker. Do not call unused. "
                "After its result, return the supplied marker. If it fails, explain the failure without retrying."}]}
    first = await request(client, "/v1/responses", body)
    assert len(calls(first)) == 1 and calls(first)[0]["name"] == "lookup", first
    return {"body": body, "first": first, "session": session}


async def follow(client, state, case):
    body, first = state["body"], state["first"]
    client = ScopedClient(client, state["session"])
    marker = "RESILIENCE_" + uuid4().hex
    output = {"type": "function_call_output", "call_id": calls(first)[0]["call_id"], "output": marker}
    followup = {**body, "input": [*body["input"], *first["output"], output]}
    if case == "reordered_tools":
        followup["tools"] = [{**t, "description": "Updated description: " + t["description"]} for t in reversed(TOOLS)]
    elif case == "changed_tools":
        followup["tools"] = TOOLS[:1]
    elif case == "tool_error":
        output.update(output="TEST_TOOL_FAILURE: permission denied", is_error=True)
    elif case in {"missing_result", "rejected_then_user"}:
        if case == "missing_result":
            followup["input"].pop()
        else:
            output.update(output="User rejected the tool", is_error=True)
        followup["input"] += [
            {"role": "user", "content": "Do not use tools. Reply with this marker: " + marker},
            {"role": "developer", "content": "<total_tokens>15000000 tokens left</total_tokens>"},
        ]
    final = await request(client, "/v1/responses", followup)
    assert not calls(final), final
    text = json.dumps(final["output"], ensure_ascii=False)
    assert text and (case == "tool_error" or marker in text), text
    report = {"case": case, "model": body["model"], "session": state["session"],
              "first_response": first["id"], "response": final["id"], "passed": True}
    if case == "missing_result":
        # The recovery turn is tool-free; the next user turn can use tools again.
        again = {**followup, "tools": TOOLS, "input": [
            *followup["input"], *final["output"],
            {"role": "user", "content": "Now call lookup once with query=marker, and return its result."}]}
        next_call = await request(client, "/v1/responses", again)
        assert len(calls(next_call)) == 1, next_call
        last = await request(client, "/v1/responses", {**again, "input": [
            *again["input"], *next_call["output"],
            {"type": "function_call_output", "call_id": calls(next_call)[0]["call_id"], "output": marker}]})
        assert not calls(last) and marker in json.dumps(last["output"]), last
        report["next_turn_tools_restored"] = True
    return report


async def main(args):
    async with httpx.AsyncClient(base_url=os.environ["GATEWAY_URL"], timeout=180,
            headers={"Authorization": "Bearer " + os.environ["GATEWAY_API_KEY"]}) as client:
        if args.phase == "seed":
            states = await asyncio.gather(*(start(client, model) for model in MODELS))
            args.state.write_text(json.dumps(states, ensure_ascii=False, indent=2))
            print(json.dumps({"seeded": [s["session"] for s in states]}), flush=True)
            return
        results = []
        async def check(state, case):
            try:
                result = await follow(client, state, case)
            except Exception as exc:
                result = {"case": case, "model": state["body"]["model"], "passed": False,
                          "error": str(exc)[:1200]}
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.state.exists():
            await asyncio.gather(*(check(s, "lost_after_restart") for s in json.loads(args.state.read_text())))
        async def model_cases(model):
            for index, case in enumerate(("reordered_tools", "changed_tools", "tool_error",
                                           "missing_result", "rejected_then_user")):
                try:
                    state = await start(client, model, stream=index % 2 == 0)
                except Exception as exc:
                    result = {"case": case, "model": model, "passed": False, "error": str(exc)[:1200]}
                    results.append(result)
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                    continue
                await check(state, case)
        await asyncio.gather(*(model_cases(model) for model in MODELS))
        # Wire-level branch concurrency: this does not spawn client subagents.
        try:
            parent = await start(client, MODELS[0])
            async def branch(index):
                marker = "BRANCH_" + str(index)
                branch_client = ScopedClient(client, parent["session"], {"x-claude-code-agent-id": str(index)})
                result = await request(branch_client, "/v1/responses", {
                    "model": MODELS[0], "input": [{"role": "user", "content": "Reply exactly " + marker}]})
                assert not calls(result) and marker in json.dumps(result["output"]), result
                return result["id"]
            branch_responses = await asyncio.gather(*(branch(i) for i in range(4)))
            parent_result = await follow(client, parent, "concurrent_parent")
            parent_result.update(case="four_branches_while_parent_waits", branches=branch_responses)
        except Exception as exc:
            parent_result = {"case": "four_branches_while_parent_waits", "passed": False, "error": str(exc)[:1200]}
        results.append(parent_result)
        print(json.dumps(parent_result, ensure_ascii=False), flush=True)
        args.report.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        if not all(r["passed"] for r in results):
            raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("seed", "verify"), required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=Path("provider-resilience-report.json"))
    asyncio.run(main(parser.parse_args()))
