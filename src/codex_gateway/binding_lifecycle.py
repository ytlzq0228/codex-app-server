"""Event-driven invalidation. Never delete audit history or retained bindings."""
from sqlalchemy import update
from .models import ExecutionSession, ResponseBinding


async def invalidate_bindings(db, *, reason, api_key_id=None, worker_id=None, thread_id=None):
    if api_key_id is None and worker_id is None:
        raise ValueError('A key or Worker scope is required')
    filters = {k:v for k,v in dict(api_key_id=api_key_id,worker_id=worker_id,thread_id=thread_id).items() if v is not None}
    await db.execute(update(ExecutionSession).where(*[getattr(ExecutionSession,k)==v for k,v in filters.items()]).values(
        state='invalid', invalid_reason=reason, lease_token=None, lease_until=None, history_hashes=None))
    result=await db.execute(update(ResponseBinding).where(
        *[getattr(ResponseBinding,k)==v for k,v in filters.items()], ResponseBinding.status=='active'
    ).values(status='invalid', invalid_reason=reason, expires_at=None))
    return result.rowcount or 0
