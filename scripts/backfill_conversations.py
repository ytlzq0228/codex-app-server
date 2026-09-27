"""Backfill explicit conversation evidence; never infer success or missing usage."""
import asyncio
from sqlalchemy import select
from codex_gateway.database import SessionLocal, engine
from codex_gateway.models import UsageRecord
from codex_gateway.conversations import explicit_identity


async def main():
    updated = grouped = 0
    while True:
        async with SessionLocal() as db:
            rows = (await db.scalars(select(UsageRecord).where(
                UsageRecord.conversation_evidence.is_(None)
            ).order_by(UsageRecord.created_at, UsageRecord.id).limit(500))).all()
            if not rows:
                break
            for record in rows:
                logical, evidence = explicit_identity(record.request_params, record.request_observation,
                    record.api_key_id, record.endpoint, record.request_id)
                evidence.update(backfilled=True, execution_outcome='legacy_unknown')
                record.logical_conversation_id = logical
                record.conversation_evidence = evidence
                updated += 1
                grouped += bool(logical)
            await db.commit()
    await engine.dispose()
    print(f'Backfilled {updated} records; {grouped} have explicit logical conversations. Status and usage unchanged.')


if __name__ == '__main__':
    asyncio.run(main())
