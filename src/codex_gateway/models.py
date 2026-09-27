import enum
from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, String, JSON, Numeric, func
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

class User(Base):
    __tablename__ = "users"
    quota_granted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    username: Mapped[str] = mapped_column(String(120), primary_key=True)
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="user")
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class UserSession(Base):
    __tablename__ = "user_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(ForeignKey("users.username"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(80))
    session_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

class OAuthState(Base):
    __tablename__ = "oauth_states"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    verifier: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class ModelPrice(Base):
    __tablename__ = "model_prices"
    model: Mapped[str] = mapped_column(String(120), primary_key=True)
    input_price: Mapped[float] = mapped_column(Numeric(18, 6))
    output_price: Mapped[float] = mapped_column(Numeric(18, 6))

class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"
    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    monthly_price: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    weight: Mapped[float] = mapped_column(Numeric(18, 6), default=1, server_default="1")

class MetricSnapshot(Base):
    """Versioned aggregate observations; source events remain in their own tables."""
    __tablename__ = "metric_snapshots"
    metric: Mapped[str] = mapped_column(String(80), primary_key=True)
    bucket_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON)


class SubscriptionCost(Base):
    __tablename__ = "subscription_costs"
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    amount: Mapped[float] = mapped_column(Numeric(18, 6))

class Worker(Base):
    __tablename__ = "workers"
    owner_username: Mapped[str | None] = mapped_column(ForeignKey("users.username"), index=True)
    account_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    account_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(180), unique=True)
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
    owner_username: Mapped[str | None] = mapped_column(ForeignKey("users.username"), index=True)
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
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    invalid_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

class ExecutionSession(Base):
    """One fenced execution checkpoint per scoped logical conversation."""
    __tablename__ = "execution_sessions"
    logical_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    api_key_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("api_keys.id"), index=True)
    endpoint: Mapped[str] = mapped_column(String(32))
    worker_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    response_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="new")
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    config_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    history_hashes: Mapped[list | None] = mapped_column(JSON, nullable=True)


class UsageRecord(Base):
    __tablename__ = "usage_records"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_username: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    request_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    request_observation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    logical_conversation_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    conversation_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    history_expected_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    input_price: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    output_price: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(24, 12), nullable=True)
    request_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    api_key_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("api_keys.id"), nullable=True, index=True)
    worker_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("workers.id"), nullable=True, index=True)
    model: Mapped[str] = mapped_column(String(120))
    status_code: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    previous_response_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    thread_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class GoogleAuthConfig(Base):
    __tablename__ = "google_auth_config"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    client_id: Mapped[str] = mapped_column(String(500), default="")
    client_secret: Mapped[str] = mapped_column(String(1000), default="")
    redirect_uri: Mapped[str] = mapped_column(String(1000), default="")
    trusted_domains: Mapped[str] = mapped_column(String(2000), default="")
