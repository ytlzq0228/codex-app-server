import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from codex_gateway.audit import current_audit
from codex_gateway.diagnostics import capture_failure
from codex_gateway.model_capabilities import supports_model
from codex_gateway.config import get_settings

def test_diagnostics_exclude_exception_messages_and_local_data():
    audit = {}
    token = current_audit.set(audit)
    try:
        try:
            try:
                raise ValueError("Bearer private-token and confidential prompt")
            except ValueError as exc:
                raise RuntimeError("secret upstream response") from exc
        except RuntimeError as exc:
            capture_failure(exc)
        rendered = json.dumps(audit)
        assert "private-token" not in rendered and "confidential" not in rendered
        assert "secret upstream" not in rendered
        chain = audit["diagnostic"]["exceptions"]
        assert [x["exception"] for x in chain] == ["RuntimeError", "ValueError"]
        assert chain[0]["frames"][-1]["file"] == "test_repair_diagnostics.py"
    finally:
        current_audit.reset(token)

def test_known_account_incompatibility_is_explicit_and_configurable(monkeypatch):
    assert not supports_model(SimpleNamespace(auth_mode="chatgpt"), "gpt-6.1-sol")
    assert supports_model(SimpleNamespace(auth_mode="apiKey"), "gpt-6.1-sol")
    assert supports_model(SimpleNamespace(auth_mode="chatgpt"), "gpt-6-sol")
    monkeypatch.setattr(get_settings(), "model_auth_restrictions", {})
    assert supports_model(SimpleNamespace(auth_mode="chatgpt"), "gpt-6.1-sol")

@pytest.mark.asyncio
async def test_pinned_incompatible_worker_is_rejected_before_execution():
    from codex_gateway.main import choose_target
    from codex_gateway.auth import ApiPrincipal
    from codex_gateway.models import Worker, WorkerStatus
    from uuid import uuid4
    worker = Worker(id=uuid4(), provider="codex", enabled=True, status=WorkerStatus.ready, auth_mode="chatgpt")
    principal = ApiPrincipal(uuid4(), "test", pinned_worker_id=worker.id)
    async def get(model, key):
        return worker if model is Worker else None
    session = SimpleNamespace(get=get)
    with pytest.raises(HTTPException) as e:
        await choose_target(principal, session, model="gpt-6.1-sol")
    assert e.value.status_code == 400
    assert e.value.detail["error"]["code"] == "model_account_unsupported"

@pytest.mark.parametrize("stream", [False, True])
def test_failure_diagnostics_survive_a_new_database_session(monkeypatch, stream):
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from codex_gateway import main
    from codex_gateway.backend import BackendTarget, WorkerFailure
    from codex_gateway.database import SessionLocal
    from codex_gateway.models import UsageRecord
    from uuid import uuid4
    marker = uuid4().hex
    class Backend:
        async def complete(self, request, target):
            try:
                raise ValueError("private upstream payload")
            except ValueError as exc:
                raise WorkerFailure("Synthetic failure", code="claude_event_invalid", status=502) from exc
        async def stream(self, request, target):
            await self.complete(request, target)
            yield
        async def close(self):
            pass
    async def target(*args, **kwargs):
        return BackendTarget("development:test", "http://worker", "/workspace", provider="claude")
    settings = get_settings()
    monkeypatch.setattr(settings, "allowed_models", settings.allowed_models + ",claude-test")
    monkeypatch.setattr(main, "choose_target", target)
    with TestClient(main.app) as client:
        main.app.state.backend = Backend()
        response = client.post("/v1/chat/completions",
            headers={"Authorization": "Bearer cag_dev_local"},
            json={"model":"claude-test", "messages":[{"role":"user","content":marker}], "stream":stream})
        assert response.status_code == (200 if stream else 502)
        async def read():
            async with SessionLocal() as db:
                rows = (await db.scalars(select(UsageRecord).where(
                    UsageRecord.model=="claude-test", UsageRecord.error_code=="claude_event_invalid"
                ).order_by(UsageRecord.created_at.desc()).limit(10))).all()
                record = next(row for row in rows if marker in json.dumps(row.request_params))
                diagnostic = record.conversation_evidence["diagnostic"]
                assert any(x["exception"] == "ValueError" for x in diagnostic["exceptions"])
                assert "private upstream payload" not in json.dumps(diagnostic)
        client.portal.call(read)
