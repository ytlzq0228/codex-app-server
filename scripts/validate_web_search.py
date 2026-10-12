"""Live Codex web-search validation. Credentials are supplied in memory/env only."""
import json
import os
import asyncio
import httpx

async def verify(client, model="gpt-6-sol"):
    results=[]
    async def call(case, body, status=200):
        r=await client.post("/v1/responses",json={"model":model,**body})
        assert r.status_code==status, (case,r.status_code,r.text[:500])
        if status!=200:
            data=r.json()
            results.append({"case":case,"status":"passed","http":status,"error":data.get("error",{}).get("code")})
            print(json.dumps(results[-1]),flush=True)
            return data
        if body.get("stream"):
            events=[json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
            assert events and events[-1]["type"]=="response.completed", (case,events[-2:])
            assert [e["sequence_number"] for e in events]==list(range(len(events)))
            data=events[-1]["response"]
            for e in events:
                if e["type"]=="response.output_item.done":
                    assert data["output"][e["output_index"]]==e["item"]
        else:data=r.json()
        assert data["status"]=="completed"
        results.append({"case":case,"status":"received","response_id":data["id"],
                        "searches":sum(i["type"]=="web_search_call" for i in data["output"])})
        print(json.dumps(results[-1]),flush=True)
        return data
    def searches(data):return [i for i in data["output"] if i["type"]=="web_search_call"]
    prompt="Use native web search once to find the official Python asyncio.TaskGroup documentation. Give a short answer with a Markdown source link. If search is unavailable reply SEARCH_UNAVAILABLE."
    first=await call("live_stream",{"input":prompt,"tools":[{"type":"web_search"}],
        "include":["web_search_call.action.sources"],"stream":True})
    assert searches(first) and searches(first)[0]["action"].get("sources")
    off=await call("resume_search_disabled",{"previous_response_id":first["id"],"input":prompt,
        "tools":[{"type":"web_search"}],"tool_choice":"none"})
    assert not searches(off) and "SEARCH_UNAVAILABLE" in json.dumps(off)
    opened=await call("resume_open_and_find",{"previous_response_id":off["id"],
        "input":"Use native web tools to open https://docs.python.org/3/library/asyncio-task.html and find TaskGroup on that page. Answer briefly with a Markdown link to that exact URL.",
        "tools":[{"type":"web_search"}],"stream":True})
    assert any(i["action"]["type"]=="open_page" for i in searches(opened))
    assert any(i["action"]["type"]=="find_in_page" for i in searches(opened))
    assert any(c.get("annotations") for i in opened["output"] if i["type"]=="message" for c in i["content"])
    tools=[{"type":"web_search","external_web_access":False},{"type":"function","name":"validation_marker",
       "description":"Returns a verification marker. Call once after searching.",
       "parameters":{"type":"object","properties":{},"additionalProperties":False}}]
    mixed=await call("cached_mixed",{"input":prompt+" After searching, call validation_marker exactly once. Wait for its result. The final answer must contain the exact marker returned by the tool.","tools":tools,"stream":True})
    calls=[i for i in mixed["output"] if i["type"]=="function_call"]
    assert searches(mixed) and len(calls)==1
    reply={"previous_response_id":mixed["id"],"input":[{"type":"function_call_output","call_id":calls[0]["call_id"],"output":"WEB_SEARCH_DEPLOYMENT_OK"}]}
    await call("reject_changed_search",dict(reply,tools=[{"type":"web_search"},tools[1]]),400)
    final=await call("client_tool_reply",dict(reply,tools=tools,stream=True))
    assert "WEB_SEARCH_DEPLOYMENT_OK" in json.dumps(final)
    plain=await call("ordinary_request",{"input":"Reply exactly ORDINARY_OK"})
    assert "ORDINARY_OK" in json.dumps(plain) and not searches(plain)
    await call("unsupported_option",{"input":"hi","tools":[{"type":"web_search","filters":{"allowed_domains":["python.org"]}}]},400)
    history=[{"role":"user","content":prompt},*first["output"],{"role":"user","content":"Reply exactly HISTORY_OK; no additional searches needed."}]
    replay=await call("full_history_search_items",{"input":history,"tools":[{"type":"web_search"}]})
    assert "HISTORY_OK" in json.dumps(replay)
    for result in results:
        result["status"] = "passed"
    print(json.dumps({"validation":"passed","cases":len(results)}),flush=True)
    return results

async def main():
    async with httpx.AsyncClient(base_url=os.environ["GATEWAY_URL"],
        headers={"Authorization":"Bearer "+os.environ["GATEWAY_API_KEY"]},timeout=180) as client:
        await verify(client,os.environ.get("TEST_MODEL","gpt-6-sol"))
if __name__=="__main__":asyncio.run(main())
