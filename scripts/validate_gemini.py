"""Run inside the test gateway container; creates and disables a temporary key within the administrator's existing capacity."""
import asyncio
from datetime import datetime, timezone
from uuid import uuid4
import httpx
from sqlalchemy import select
from codex_gateway.config import get_settings
from codex_gateway.database import SessionLocal
from codex_gateway.models import ApiKey, User, Worker, ResponseBinding
from codex_gateway.security import generate_api_key, hash_api_key

async def main():
    settings = get_settings()
    raw, prefix = generate_api_key()
    username = settings.admin_username
    async with SessionLocal() as db:
        from codex_gateway.quota import ensure_capacity
        await ensure_capacity(db, username)
        key = ApiKey(name="integration-validation", prefix=prefix,
                     key_hash=hash_api_key(raw, settings.key_pepper.get_secret_value()),
                     owner_username=username, enabled=True)
        db.add(key)
        await db.commit()
        await db.refresh(key)
        key_id = key.id
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=180,
                                     headers={"Authorization": "Bearer " + raw}) as client:
            async def post(path, data):
                response = await client.post(path, json=data)
                assert response.status_code == 200, (response.status_code, response.text)
                return response
            body = {"model": "gemini-3.8-flash-high", "input": "Remember river-619. Reply ACK river-619 only. Do not use tools."}
            first = (await post("/v1/responses", body)).json()
            rid = first["id"]
            print("PASS Responses text", flush=True)
            second = (await post("/v1/responses", {"model": body["model"], "previous_response_id": rid,
                         "input": "What marker did I give you? Reply only with it. Do not use tools."})).json()
            assert "river-619" in str(second["output"])
            usage = second["usage"]
            assert usage["input_tokens_details"]["cached_tokens"] <= usage["input_tokens"]
            print("PASS native continuation and normalized cache usage", flush=True)
            response = await client.post("/v1/responses", json={"model": "gpt-6-sol", "previous_response_id": rid, "input": "hello"})
            assert response.status_code == 400 and response.json()["error"]["code"] == "provider_mismatch"
            print("PASS provider switch rejected", flush=True)
            response = await post("/v1/chat/completions", {"model": body["model"], "stream": True,
                "messages": [{"role": "user", "content": "Reply OK only. Do not use tools."}]})
            assert "[DONE]" in response.text and '"error"' not in response.text, response.text
            print("PASS Chat Completions stream", flush=True)
            response = await post("/v1/responses", {"model": body["model"], "stream": True, "input": "Reply OK only. Do not use tools."})
            assert "response.completed" in response.text and "response.failed" not in response.text, response.text
            print("PASS Responses stream", flush=True)
            response = await client.post("/v1/responses", json={"model": body["model"], "input": "hello",
                "parallel_tool_calls": True})
            assert response.status_code == 400 and response.json()["error"]["param"] == "parallel_tool_calls"
            print("PASS unsupported parallel tools rejected", flush=True)
            async with SessionLocal() as db:
                available = await db.scalar(select(Worker.id).where(Worker.provider == "codex", Worker.enabled.is_(True), Worker.status.in_(["ready", "busy"])).limit(1))
            if available:
                response = await post("/v1/responses", {"model": "gpt-6-sol", "input": "Reply OK only. Do not use tools."})
                assert response.json()["status"] == "completed"
                print("PASS existing Codex real inference", flush=True)
            else:
                print("SKIP Codex real inference: existing workers are disabled or unavailable", flush=True)
        async with SessionLocal() as db:
            binding = await db.get(ResponseBinding, rid)
            worker = await db.get(Worker, binding.worker_id)
            assert binding.provider == worker.provider == "gemini"
            print("PASS persisted provider binding", flush=True)
        async with httpx.AsyncClient(timeout=90, headers={"Authorization": "Bearer " + settings.app_server_token.get_secret_value()}) as client:
            payload = {"prompt": "Write 500 gardening tips. Do not use tools.", "model": body["model"], "workspace": "/workspace/integration-test"}
            async with client.stream("POST", worker.endpoint + "/turn", json=payload) as response:
                assert response.status_code == 200
                await asyncio.sleep(1)
            await asyncio.sleep(7)
            payload["prompt"] = "Reply OK only. Do not use tools."
            response = await client.post(worker.endpoint + "/turn", json=payload)
            assert response.status_code == 200 and '"done": true' in response.text, response.text
            print("PASS cancellation followed by successful new request", flush=True)
    finally:
        async with SessionLocal() as db:
            key = await db.get(ApiKey, key_id)
            key.enabled = False
            key.deleted_at = datetime.now(timezone.utc)
            await db.commit()

if __name__ == "__main__":
    asyncio.run(main())
