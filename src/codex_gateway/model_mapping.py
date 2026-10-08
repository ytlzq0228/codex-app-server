"""Resolve one upstream name without changing the public model identity."""
from .models import ModelMapping


async def resolve_model(db, model):
    mapping = await db.get(ModelMapping, model)
    return mapping.upstream_model if mapping else model


async def model_available(db, model):
    from .config import get_settings
    return model in get_settings().public_models() or await db.get(ModelMapping, model) is not None


async def public_models(db):
    from sqlalchemy import select
    from .config import get_settings
    mappings = await db.scalars(select(ModelMapping.model).order_by(ModelMapping.model))
    return list(dict.fromkeys([*get_settings().public_models(), *mappings]))
