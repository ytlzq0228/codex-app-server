import enum
from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass

class WorkerStatus(str, enum.Enum):
    offline = "offline"
    ready = "ready"
    busy = "busy"
    draining = "draining"
    error = "error"


class AdminUser(Base):
    __tablename__ = "admin_users"
    username: Mapped[str] = mapped_column(String(120), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class Worker(Base):
    __tablename__ = "workers"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    container_name: Mapped[str] = mapped_column(String(180), unique=True)
    endpoint: Mapped[str] = mapped_column(String(500))
    status: Mapped[WorkerStatus] = mapped_column(Enum(WorkerStatus), default=WorkerStatus.offline)
    auth_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plan_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    scheduling_mode: Mapped[str] = mapped_column(String(16), default="pooled")
    pinned_worker_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("workers.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

class ResponseBinding(Base):
    __tablename__ = "response_bindings"
    response_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    api_key_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("api_keys.id"), index=True)
    worker_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("workers.id"), index=True)
    thread_id: Mapped[str] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class UsageRecord(Base):
    __tablename__ = "usage_records"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    api_key_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("api_keys.id"), nullable=True, index=True)
    worker_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("workers.id"), nullable=True, index=True)
    model: Mapped[str] = mapped_column(String(120))
    status_code: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
