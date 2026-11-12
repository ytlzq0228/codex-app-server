"""Serialize admission decisions across SSO callbacks and administrators."""
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .models import AdmissionConfig


async def lock_policy(db):
    # Upsert also serializes first use, before the singleton row exists.
    await db.execute(insert(AdmissionConfig).values(id=1, enabled=False)
                     .on_conflict_do_nothing(index_elements=["id"]))
    return await db.scalar(select(AdmissionConfig).where(AdmissionConfig.id == 1)
                           .with_for_update())
