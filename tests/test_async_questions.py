import copy
import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from codex_gateway.async_questions import async_questions, question_call
from codex_gateway.backend import AppServerBackend, BackendTarget, WorkerFailure
from codex_gateway.client_tools import definitions, ToolProtocolError
from codex_gateway.config import Settings
from codex_gateway.schemas import ResponseRequest


QUESTIONS = [{"title": "选择主题？", "options": ["浅色", "深色"]},
             {"title": "还有什么建议？"}]
TOOL = {"type": "function", "name": "request_user_input_async", "parameters": {
    "type": "object", "required": ["questions"],
    "properties": {"questions": {"type": "array", "minItems": 1}},
    "additionalProperties": False}}
TARGET = BackendTarget("key:worker", "ws://worker", "/workspace")


def request(**kwargs):
    return ResponseRequest(model="test", input="Ask two questions", tools=[
        {"type": "namespace", "name": "functions", "tools": [copy.deepcopy(TOOL)]}], **kwargs)


def notification(item_id="question-1", **updates):
    item = {"type": "agentMessage", "id": item_id, "delivery": "async",
            "text": "Questions", "questions": copy.deepcopy(QUESTIONS)}
    item.update(updates)
    return {"method": "item/completed", "params": {
        "threadId": "thread-a", "turnId": "turn-a", "item": item}}


@pytest.fixture
def backend():
    backend = AppServerBackend(Settings())
    backend.tool_sessions.ttl = 1
    backend.rpc_calls = []
    backend.notifications = [notification(), notification(),
        {"method": "item/agentMessage/delta", "params": {"delta": "Form sent."}},
        {"method": "turn/completed", "params": {"turn": {"id": "turn-a", "status": "completed"}}}]

    class Server:
        async def call(self, method, params):
            backend.rpc_calls.append((method, params))
            return {"thread": {"id": "thread-a"}, "turn": {"id": "turn-a"}}

        async def messages(self):
            for event in backend.notifications:
                yield event

        async def reject_server_request(self, message):
            pytest.fail("Async notification must not be rejected as an RPC")

        # No websocket: sending a synthetic RPC reply must fail this test.

    class Pool:
        @asynccontextmanager
        async def lease(self, *args):
            yield Server(), 0

        async def invalidate(self, *args):
            pass

        async def close(self):
            pass

    backend.pool = Pool()
    return backend


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_form_relay_acknowledgement_and_later_answer(backend, streaming):
    async def run(req):
        if streaming:
            events = [e async for e in backend.stream(req, TARGET)]
            return [e.tool_call for e in events if e.tool_call], "".join(e.delta or "" for e in events)
        result = await backend.complete(req, TARGET)
        return result.tool_calls, result.text

    try:
        calls, text = await run(request())
        assert text == "" and len(calls) == 1
        call = calls[0]
        assert call["namespace"] == "functions"
        assert call["name"] == "request_user_input_async"
        assert json.loads(call["arguments"]) == {"questions": QUESTIONS}
        acknowledgement = ResponseRequest(model="test", input=[{
            "type": "function_call_output", "call_id": call["call_id"],
            "output": '{"accepted":true}'}])
        with pytest.raises(ToolProtocolError):
            backend.continuation_target(acknowledgement, "another-key")
        calls, text = await run(acknowledgement)
        assert calls == [] and text == "Form sent."
        assert len([c for c in backend.rpc_calls if c[0] == "turn/start"]) == 1
        with pytest.raises(WorkerFailure, match="already been submitted"):
            await run(acknowledgement)

        # A later answer starts a regular user turn on the same Worker thread.
        backend.notifications = [
            {"method": "item/agentMessage/delta", "params": {"delta": "Dark selected."}},
            {"method": "turn/completed", "params": {"turn": {"id": "turn-a", "status": "completed"}}}]
        calls, text = await run(ResponseRequest(model="test", previous_response_id="thread-a",
            input=[{"role": "user", "content": "深色"}]))
        assert calls == [] and text == "Dark selected."
        assert any(method == "thread/resume" for method, _ in backend.rpc_calls)
        assert "深色" in json.dumps(backend.rpc_calls[-1][1], ensure_ascii=False)
    finally:
        await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("req", [ResponseRequest(model="test", input="Ask"), request(tool_choice="none")])
async def test_missing_or_disabled_form_tool_falls_back_to_text(backend, req):
    try:
        result = await backend.complete(req, TARGET)
        assert not result.tool_calls
        assert result.text.count("选择主题？") == 1
        assert "- 浅色" in result.text and "还有什么建议？" in result.text
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_other_thread_and_turn_and_ordinary_messages_are_not_forms(backend):
    wrong_thread, wrong_turn = notification(), notification()
    wrong_thread["params"]["threadId"] = "other"
    wrong_turn["params"]["turnId"] = "other"
    backend.notifications = [wrong_thread, wrong_turn, notification(delivery=None),
                             *backend.notifications[-2:]]
    try:
        result = await backend.complete(request(), TARGET)
        assert not result.tool_calls and result.text == "Form sent."
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_invalid_form_is_explicit_request_failure(backend):
    backend.notifications = [notification(questions=[])]
    try:
        with pytest.raises(WorkerFailure, match="invalid async questions") as error:
            await backend.complete(request(), TARGET)
        assert error.value.kind == "request"
    finally:
        await backend.close()


def test_client_schema_namespace_and_question_validation():
    item = notification()["params"]["item"]
    assert async_questions(item) == QUESTIONS
    item["type"] = "AgentMessage"
    assert async_questions(item) == QUESTIONS
    specs = definitions(request())
    specs[0]["schema"]["properties"]["questions"]["maxItems"] = 1
    with pytest.raises(ToolProtocolError, match="client form schema"):
        question_call(specs, QUESTIONS)
    specs[0]["namespace"] = "unrelated"
    assert question_call(specs, QUESTIONS) is None
    for invalid in [None, [], [{"title": ""}], [{"title": "Q", "options": "bad"}]]:
        with pytest.raises(ToolProtocolError):
            async_questions({**item, "questions": invalid})


@pytest.mark.asyncio
async def test_form_ack_timeout_releases_pending_session(backend):
    backend.tool_sessions.ttl = 0.01
    try:
        first = await backend.complete(request(), TARGET)
        assert len(first.tool_calls) == 1
        runs = list(backend.tool_sessions.runs)
        await asyncio.wait_for(asyncio.gather(*(run.task for run in runs)), 1)
        assert not backend.tool_sessions.pending and not backend.tool_sessions.runs
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_worker_question_reaches_responses_sse_as_client_tool(backend, monkeypatch):
    from codex_gateway import main
    from codex_gateway.auth import ApiPrincipal
    saved = []

    async def save(*args, **kwargs):
        saved.append(args[6])

    monkeypatch.setattr(main, "save_usage", save)
    try:
        events = [json.loads(raw.split("data: ", 1)[1]) async for raw in
                  main.response_stream(request(), backend, ApiPrincipal(None, "test"), TARGET, None)]
        output = events[-1]["response"]["output"]
        assert len(output) == 1
        assert output[0]["type"] == "function_call"
        assert output[0]["namespace"] == "functions"
        assert output[0]["name"] == "request_user_input_async"
        assert json.loads(output[0]["arguments"]) == {"questions": QUESTIONS}
        assert saved[-1].tool_calls == output
        assert any(e["type"] == "response.function_call_arguments.done" for e in events)
    finally:
        await backend.close()
