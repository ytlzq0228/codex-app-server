import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .config import Settings, get_settings
from .database import get_session
from .models import ApiKey
from .security import keys_equal

bearer = HTTPBearer(auto_error=False)

@dataclass(frozen=True)
class ApiPrincipal:
    key_id: UUID | None
    name: str
    scheduling_mode: str = "pooled"
    pinned_worker_id: UUID | None = None

async def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> ApiPrincipal:
    supplied = credentials.credentials if credentials else ""
    dev_key = settings.dev_api_key.get_secret_value() if settings.dev_api_key else ""
    if supplied and dev_key and hmac.compare_digest(supplied, dev_key):
        return ApiPrincipal(key_id=None, name="development")

    parts = supplied.split("_", 2)
    if len(parts) == 3 and parts[0] == "cag":
        record = await session.scalar(select(ApiKey).where(ApiKey.prefix == parts[1], ApiKey.enabled.is_(True), ApiKey.deleted_at.is_(None)))
        if record and keys_equal(supplied, record.key_hash, settings.key_pepper.get_secret_value()):
            record.last_used_at = datetime.now(timezone.utc)
            await session.commit()
            return ApiPrincipal(key_id=record.id, name=record.name, scheduling_mode=record.scheduling_mode, pinned_worker_id=record.pinned_worker_id)

    raise HTTPException(401, detail={"error": {"message": "Invalid authentication credentials", "type": "invalid_request_error", "code": "invalid_api_key"}}, headers={"WWW-Authenticate": "Bearer"})
