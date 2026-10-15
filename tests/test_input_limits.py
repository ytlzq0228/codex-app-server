from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from codex_gateway.input_limits import CODEX_MAX_INPUT_CHARACTERS as LIMIT, input_length_error
from codex_gateway.backend import AppServerBackend, BackendTarget, WorkerFailure, classify_worker_failure
from codex_gateway.app_server import AppServerError
from codex_gateway.config import get_settings
from codex_gateway.schemas import ResponseRequest, ChatCompletionRequest
from codex_gateway.auth import ApiPrincipal
import codex_gateway.main as main

def test_limit_counts_text_and_accepts_boundary():
    assert input_length_error([{"type":"text","text":"中"*LIMIT}]) is None
    assert input_length_error([{"type":"text","text":"x"*LIMIT},{"type":"text","text":"y"}])
    assert input_length_error([{"type":"image","url":"x"*(LIMIT+1)}]) is None

@pytest.mark.asyncio
async def test_backend_rejects_without_opening_connection():
    backend=AppServerBackend(get_settings())
    class Pool:
        def lease(self,*args):raise AssertionError("must not contact worker")
    backend.pool=Pool()
    with pytest.raises(WorkerFailure) as caught:
        await backend.complete(ResponseRequest(model="gpt-6-sol",input="x"*(LIMIT+1)),BackendTarget("k","ws://test","/workspace/k"))
    assert (caught.value.kind,caught.value.status,caught.value.code,caught.value.safe_to_retry)==("request",400,"input_too_long",False)

@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False,True])
async def test_upstream_rejection_and_full_history_fallback(fallback):
    calls=[]
    class Server:
        async def call(self,method,params):
            calls.append(method)
            if method=="account/read":return {"account":{}}
            if method=="thread/resume":raise AppServerError("thread not found")
            if method=="thread/start":return {"thread":{"id":"thread"}}
            if method=="turn/start":raise AppServerError("turn/start: Input exceeds the maximum length of 1048576 characters.")
            raise AssertionError(method)
    class Pool:
        @asynccontextmanager
        async def lease(self,*args):yield Server(),0
    backend=AppServerBackend(get_settings());backend.pool=Pool()
    req=ResponseRequest(model="gpt-6-sol",input="x"*(LIMIT+1) if fallback else "short")
    if fallback:
        req.previous_response_id="old";req._execution_auto_resume=True;req._execution_input_text="delta"
    with pytest.raises(WorkerFailure) as caught:
        await backend.complete(req,BackendTarget("k","ws://test","/workspace/k"))
    assert (caught.value.code,caught.value.status,caught.value.safe_to_retry)==("input_too_long",400,False)
    assert ("turn/start" in calls) is not fallback
    assert caught.value.kind == "request"
    if not fallback:
        assert classify_worker_failure(str(caught.value)) == "request"


def test_length_check_uses_execution_delta_not_full_history():
    req = ResponseRequest(model="gpt-6-sol", input="x"*(LIMIT+1))
    assert input_length_error(req.worker_input())
    req._execution_input_text = "small delta"
    assert input_length_error(req.worker_input()) is None

@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint",["chat","responses"])
@pytest.mark.parametrize("stream",[False,True])
async def test_api_rejects_before_worker_selection(monkeypatch,endpoint,stream):
    import codex_gateway.providers as providers
    import codex_gateway.model_mapping as mapping
    import codex_gateway.cluster as cluster
    monkeypatch.setattr(main,"model_available",AsyncMock(return_value=True))
    monkeypatch.setattr(providers,"authorize_model",AsyncMock())
    monkeypatch.setattr(mapping,"resolve_model",AsyncMock(return_value="gpt-6-sol"))
    monkeypatch.setattr(main,"validate_grammars",AsyncMock())
    monkeypatch.setattr(main,"validate_pending_worker",AsyncMock())
    monkeypatch.setattr(main,"release_request_session",AsyncMock())
    async def recover(req,*args,**kw):return req,None,None
    async def prepare(req,*args,**kw):return req,None
    monkeypatch.setattr(cluster,"recoverable_pending_route",recover)
    monkeypatch.setattr(main,"prepare_execution",prepare)
    choose=AsyncMock(side_effect=AssertionError("worker must not be selected"))
    monkeypatch.setattr(main,"choose_execution_target",choose)
    if endpoint=="chat":
        body=ChatCompletionRequest(model="gpt-6-sol",stream=stream,messages=[{"role":"user","content":"x"*(LIMIT+1)}])
        fn=main.create_chat_completion
    else:
        body=ResponseRequest(model="gpt-6-sol",stream=stream,input="x"*(LIMIT+1))
        fn=main.create_response
    with pytest.raises(HTTPException) as caught:
        await fn(body,principal=ApiPrincipal(None,"test"),backend=object(),session=object())
    assert caught.value.status_code==400
    assert caught.value.detail["error"]["code"]=="input_too_long"
    choose.assert_not_awaited()

@pytest.mark.asyncio
async def test_input_rejection_never_quarantines_or_retries(monkeypatch):
    backend=AppServerBackend(get_settings())
    quarantine=AsyncMock(side_effect=AssertionError("no health probe"))
    retry=AsyncMock(side_effect=AssertionError("no retry"))
    monkeypatch.setattr(main,"quarantine_worker",quarantine)
    monkeypatch.setattr(main,"retry_target",retry)
    with pytest.raises(HTTPException) as caught:
        await main.complete_with_failover(ResponseRequest(model="gpt-6-sol",input="x"*(LIMIT+1)),backend,ApiPrincipal(None,"test"),BackendTarget("k","ws://test","/workspace/k"),allow_retry=True)
    assert caught.value.status_code==400
    quarantine.assert_not_awaited();retry.assert_not_awaited()

