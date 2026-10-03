"""Claude provisional usage, replaced atomically by a successful run result."""
import hashlib
from decimal import Decimal

from sqlalchemy import select, text

from .billing import priced_amount
from .models import ModelPrice, UsageRecord, Worker

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def observe_usage(target, event):
    from .audit import current_audit, track_backend
    if target.provider != "claude" or not event.usage_accounting:
        return
    audit = current_audit.get()
    if audit is not None:
        track_backend(target, event.thread_id)
        audit["claude_usage"] = {
            **event.usage_accounting,
            "tokens": {name: getattr(event, name) for name in TOKEN_FIELDS},
        }


def snapshot(result, audit):
    if result is not None and result.usage_accounting:
        return {**result.usage_accounting,
                "tokens": {name: getattr(result, name) for name in TOKEN_FIELDS}}
    return (audit or {}).get("claude_usage")


def supersede(record, final_request_id):
    evidence = dict(record.conversation_evidence or {})
    evidence["usage_accounting"] = {
        **evidence["usage_accounting"], "superseded_by": final_request_id,
    }
    record.conversation_evidence = evidence
    for name in TOKEN_FIELDS:
        setattr(record, name, 0)
    record.cost_usd = Decimal(0)


async def apply_usage(session, record, usage):
    """Keep original observations in evidence; totals/cost contain billable usage.

    Run identity is internal and unique per CLI execution, never a client ID or
    a reusable thread ID. Locking also handles a delayed provisional insert after
    a final result has already committed on the other gateway node.
    """
    if record.provider != "claude" or not usage or not usage.get("run_id"):
        return
    if record.worker_id:
        await session.scalar(select(Worker).where(Worker.id == record.worker_id).with_for_update())
    else:
        lock_id = int.from_bytes(hashlib.sha256(usage["run_id"].encode()).digest()[:8], "big", signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})
    for name in TOKEN_FIELDS:
        setattr(record, name, usage["tokens"][name])
    price = await session.get(ModelPrice, record.model)
    record.input_price = price.input_price if price else None
    record.output_price = price.output_price if price else None
    record.cache_read_price = price.cache_read_price if price else None
    record.cache_write_price = price.cache_write_price if price else None
    record.cost_usd = priced_amount(*(getattr(record, n) for n in TOKEN_FIELDS), price) if price else None
    record.conversation_evidence = {**(record.conversation_evidence or {}), "usage_accounting": usage}
    previous = list((await session.scalars(select(UsageRecord).where(
        UsageRecord.provider == "claude",
        UsageRecord.api_key_id == record.api_key_id,
        UsageRecord.worker_id == record.worker_id,
        UsageRecord.conversation_evidence["usage_accounting"]["run_id"].as_string() == usage["run_id"],
    ).with_for_update())).all())
    final = next((row for row in previous if row.conversation_evidence["usage_accounting"].get("final")), None)
    if final:
        supersede(record, final.request_id)
    elif usage.get("final"):
        for row in previous:
            supersede(row, record.request_id)
