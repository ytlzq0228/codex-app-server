import json
from contextlib import asynccontextmanager
import pytest
from codex_gateway.backend import AppServerBackend, BackendTarget
from codex_gateway.schemas import ResponseRequest, BackendStreamEvent, BackendResult
from codex_gateway.client_tools import definitions, ToolProtocolError
from codex_gateway.config import Settings
from codex_gateway.worker_policy import thread_policy
from codex_gateway.web_search import mode, native_item, annotations
from codex_gateway.execution import configuration, normal_item
from codex_gateway.tool_sessions import ToolSessions

TOOL = {"type":"function","name":"lookup","parameters":{"type":"object","properties":{}}}
TARGET = BackendTarget("key:worker","ws://worker","/workspace")

def request(**kw):
    return ResponseRequest(**{"model":"gpt-test","input":"hello","tools":[{"type":"web_search"}],**kw})

@pytest.mark.parametrize("tools,choice,expected", [
    (None,None,"disabled"),([{"type":"web_search"}],None,"live"),
    ([{"type":"web_search","external_web_access":False}],None,"cached"),
    ([{"type":"web_search"}],"none","disabled")])
def test_policy_and_aliases(tools,choice,expected):
    req=request(tools=tools,tool_choice=choice)
    assert mode(req)==expected
    policy=thread_policy(req)
    assert policy["config"]["web_search"]==expected
    assert not policy["config"]["features.shell_tool"]
    assert thread_policy()["config"]["web_search"]=="disabled"
    assert definitions(request(tools=[{"type":"web_search"},TOOL]))[0]["alias"]=="gateway_client_0"

@pytest.mark.parametrize("kw", [
    {"model":"claude-test"},{"model":"gemini-test"},
    {"tools":[{"type":"web_search","filters":{}}]},
    {"tools":[{"type":"web_search","external_web_access":"false"}]},
    {"tools":[{"type":"web_search"},{"type":"web_search"}]},
    {"tools":[{"type":"namespace","name":"bad","tools":[{"type":"web_search"}]}]},
    {"input":[{"type":"additional_tools","tools":[{"type":"web_search"}]}]},
    {"max_tool_calls":1}])
def test_invalid_search_contracts(kw):
    assert request(**kw).unsupported()

def test_history_and_config():
    native={"id":"ws_a","type":"web_search_call","status":"completed","action":{"type":"search","query":"x"}}
    req=request(input=[native,{"role":"user","content":"next"}])
    assert req.unsupported() is None
    assert "ASSISTANT WEB SEARCH" in req.input_text()
    assert configuration(request())!=configuration(request(tools=[]))
    assert configuration(request())!=configuration(request(tools=[{"type":"web_search","external_web_access":False}]))
    assert normal_item(native)==normal_item({**native,"action":{**native["action"],"sources":[]}})

def test_search_conversion_and_citations():
    item,sources=native_item({"id":"ws_a","action":{"type":"findInPage","pattern":"x"},
        "results":[{"url":"https://example.com/a","title":"Source"}]},True)
    assert item["action"]=={"type":"find_in_page","pattern":"x","url":"https://example.com/a"}
    text="See [source](https://example.com/a#part) and [other](https://example.org/)."
    anns=annotations(text,sources)
    assert len(anns)==1 and text[anns[0]["start_index"]:anns[0]["end_index"]]=="[source](https://example.com/a#part)"

@pytest.mark.asyncio
async def test_real_backend_event_mapping_and_resume_policy():
    backend=AppServerBackend(Settings())
    calls=[]
    class Server:
        async def call(self,method,params):
            calls.append((method,params))
            return {"thread":{"id":"thread-a"},"turn":{"id":"turn-a"}}
        async def messages(self):
            for method,item in [("item/started",{"type":"webSearch","id":"ws_a"}),
                ("item/completed",{"type":"webSearch","id":"ws_a","action":{"type":"search","query":"x"},
                   "results":[{"url":"https://example.com","title":"Source"}]})]:
                yield {"method":method,"params":{"item":item}}
            yield {"method":"item/agentMessage/delta","params":{"delta":"found"}}
            yield {"method":"turn/completed","params":{"turn":{"status":"completed"}}}
    class Pool:
        @asynccontextmanager
        async def lease(self,*args): yield Server(),0
        async def close(self): pass
    backend.pool=Pool()
    result=await backend.complete(request(),TARGET)
    assert len(result.native_items)==1 and not result.tool_calls
    assert result.web_sources[0]["title"]=="Source"
    assert calls[-1] == ("thread/unsubscribe", {"threadId": "thread-a"})
    assert next(p for m,p in calls if m=="thread/start")["config"]["web_search"]=="live"
    await backend._start_thread(Server(),request(tools=[],previous_response_id="thread-a"),"/workspace")
    assert calls[-1][1]["config"]["web_search"]=="disabled"
    await backend.close()

@pytest.mark.asyncio
async def test_search_contract_cannot_change_during_tool_reply():
    sessions=None
    async def events(req,target,run):
        call={"id":"fc_a","type":"function_call","name":"lookup","call_id":"call_a","arguments":"{}"}
        await sessions.await_result(run,call)
        yield BackendStreamEvent(tool_call=call,thread_id="thread-a")
        result=await sessions.receive_result(run)
        yield BackendStreamEvent(delta=result,thread_id="thread-a",done=True)
    sessions=ToolSessions(events,ttl=2)
    original=request(tools=[{"type":"web_search"},TOOL])
    try:
        first=[e async for e in sessions.stream(original,TARGET)]
        output=[{"type":"function_call_output","call_id":"call_a","output":"OK"}]
        with pytest.raises(ToolProtocolError,match="Cannot change web_search"):
            sessions.target_for(request(input=output,tools=[TOOL]),"key")
        with pytest.raises(ToolProtocolError,match="Cannot change web_search"):
            sessions.target_for(request(input=output,tools=None,tool_choice="none"),"key")
        # Omitting declarations inherits the still-running turn.
        final=[e async for e in sessions.stream(request(input=output,tools=None),TARGET)]
        assert final[-1].delta=="OK"
    finally:
        await sessions.close()

@pytest.mark.asyncio
@pytest.mark.parametrize("text_first",[False,True])
async def test_sse_output_indices_sources_and_cross_node_collection(monkeypatch,text_first):
    import codex_gateway.main as main
    from codex_gateway.cluster import collect
    from codex_gateway.auth import ApiPrincipal
    saved=[]
    async def save(*args,**kw): saved.append(args[6])
    monkeypatch.setattr(main,"save_usage",save)
    text="See [source](https://example.com/a)."
    completed={"id":"ws_a","type":"web_search_call","status":"completed",
        "action":{"type":"search","query":"x","sources":[{"type":"url","url":"https://example.com/a"}]}}
    call={"id":"fc_a","type":"function_call","name":"lookup","call_id":"call_a","arguments":"{}","status":"completed"}
    class Backend:
        async def stream(self,*args):
            if text_first: yield BackendStreamEvent(delta=text,thread_id="thread-a")
            yield BackendStreamEvent(native_item={"id":"ws_a","type":"web_search_call","status":"in_progress"},thread_id="thread-a")
            yield BackendStreamEvent(native_item=completed,thread_id="thread-a")
            if not text_first: yield BackendStreamEvent(delta=text,thread_id="thread-a")
            yield BackendStreamEvent(tool_call=call,web_sources=[{"url":"https://example.com/a","title":"Source"}],thread_id="thread-a")
    backend=Backend()
    req=request(include=["web_search_call.action.sources"])
    raw=[e async for e in main.response_stream(req,backend,ApiPrincipal(None,"test"),TARGET,None)]
    events=[json.loads(e.split("data: ",1)[1]) for e in raw]
    assert [e["sequence_number"] for e in events]==list(range(len(events)))
    final=events[-1]["response"]
    assert [i["type"] for i in final["output"]]==["message","web_search_call","function_call"]
    for e in events:
        if e["type"]=="response.output_item.done":
            assert final["output"][e["output_index"]]==e["item"]
    assert final["output"][0]["content"][0]["annotations"]
    assert final["output"][1]["action"]["sources"]
    # NDJSON serialization preserves native items on the owner/ingress boundary.
    async def wire():
        async for e in backend.stream():
            yield BackendStreamEvent.model_validate_json(e.model_dump_json())
    remote=await collect(wire())
    assert remote.native_items==[completed] and remote.tool_calls==[call]
    without_sources=main.response_object("r",request(),remote)
    assert "sources" not in without_sources["output"][1]["action"]
