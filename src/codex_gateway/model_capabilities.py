"""Known account/model incompatibilities, configurable without model substitution."""
from .config import get_settings

def supports_model(worker, upstream):
    denied = get_settings().model_auth_restrictions.get(upstream, [])
    return (getattr(worker, "auth_mode", None) or "").lower() not in denied

async def upstream_name(db, model):
    from .model_mapping import resolve_model
    return await resolve_model(db, model)

async def filter_visible_models(db, principal, models):
    from sqlalchemy import select
    from .models import Worker, WorkerStatus
    from .providers import provider_for
    workers = list((await db.scalars(select(Worker).where(
        Worker.enabled.is_(True), Worker.status.in_([WorkerStatus.ready, WorkerStatus.busy])))).all())
    if principal.pinned_worker_id:
        workers = [w for w in workers if w.id == principal.pinned_worker_id]
    result = []
    for model in models:
        upstream = await upstream_name(db, model)
        if upstream not in get_settings().model_auth_restrictions:
            result.append(model)
            continue
        if any(w.provider == provider_for(model) and supports_model(w, upstream) for w in workers):
            result.append(model)
    return result
