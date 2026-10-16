import asyncio
import pytest
from fastapi import HTTPException
from codex_gateway.cancellations import Cancellations, operation_id

@pytest.mark.asyncio
async def test_disconnect_does_not_interrupt_cancellation_and_retry_is_idempotent():
    operations = Cancellations(wait_seconds=0)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def cancel():
        calls.append(1)
        started.set()
        await release.wait()
        return True
    assert (await operations.run("op", cancel))["status"] == "pending"
    await started.wait()
    assert (await operations.run("op", cancel, status_only=True))["status"] == "pending"
    release.set()
    await asyncio.sleep(0)
    assert (await operations.run("op", cancel))["cancelled"]
    assert calls == [1]

@pytest.mark.asyncio
async def test_ingress_cancellation_keeps_owner_running():
    operations = Cancellations(wait_seconds=10)
    started, release = asyncio.Event(), asyncio.Event()
    async def cancel():
        started.set()
        await release.wait()
        return True
    ingress = asyncio.create_task(operations.run("op", cancel))
    await started.wait()
    ingress.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ingress
    assert not operations.entries["op"][1].cancelled()
    release.set()
    assert (await operations.run("op", cancel))["cancelled"]

@pytest.mark.asyncio
async def test_claimed_result_is_conflict_and_unknown_status_cannot_start_work():
    operations = Cancellations()
    async def cancel():
        return False
    with pytest.raises(HTTPException) as e:
        await operations.run("unknown", cancel, status_only=True)
    assert e.value.detail["error"]["code"] == "tool_cancellation_unknown"
    assert not operations.entries
    with pytest.raises(HTTPException) as e:
        await operations.run("op", cancel)
    assert e.value.status_code == 409

def test_operation_identity_is_scoped_to_key_checkpoint_and_action():
    payload = dict(key_id="key", logical_id="logical", response_id="response")
    original = operation_id("abandon", payload)
    for key in payload:
        assert operation_id("abandon", {**payload, key: "different"}) != original
    assert operation_id("supersede", payload) != original
