from functools import lru_cache
from typing import Literal
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CODEX_GATEWAY_", env_file=".env")
    database_url: str = "postgresql+asyncpg://codex:codex@localhost:5432/codex_gateway"
    key_pepper: SecretStr = SecretStr("development-only-change-me")
    backend: Literal["mock", "app_server"] = "mock"
    # No default credential: an unset value disables the development bypass entirely.
    dev_api_key: SecretStr | None = None
    admin_password: SecretStr = SecretStr("development-admin-change-me")
    admin_username: str = "admin"
    admin_session_secret: SecretStr | None = None  # Legacy setting; sessions are stored in PostgreSQL.
    # Cookie Secure flag: unset detects HTTPS from the request, true forces it on
    # (use this when a TLS proxy is not in --forwarded-allow-ips), false forces it off.
    admin_cookie_secure: bool | None = None
    auto_create_schema: bool = True
    model_name: str = ""  # Legacy environment setting; no longer exposes a model alias.
    upstream_model: str = "gpt-6-sol"
    allowed_models: str = "gpt-6-sol,gpt-6-astra,gpt-6-luna,gpt-5.6-sol,gpt-5.6-terra,gpt-5.6-luna,gpt-5.6"
    gemini_native_model_aliases: str = ""  # Explicit native CLI model aliases
    claude_native_model_aliases: str = ""
    model_providers: str = ""  # Explicit public-model:provider entries
    model_aliases: str = "gpt-5.6:gpt-5.6-sol"
    app_server_url: str = "ws://worker-1:4500"
    app_server_token: SecretStr = SecretStr("development-worker-token-change-me")
    app_server_timeout_seconds: float = 300.0
    app_server_max_message_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    workspace_host_root: str = "/worker-workspaces/worker-1"
    workspace_worker_root: str = "/workspace"
    manager_url: str = "http://worker-manager:4600"
    manager_token: SecretStr = SecretStr("development-manager-token-change-me")
    max_request_bytes: int = 20_971_520
    worker_recovery_interval_seconds: float = 30.0
    worker_failure_cooldown_seconds: int = 300
    worker_limit_cooldown_seconds: int = 1800
    response_binding_ttl_hours: int = 24  # Legacy setting; ordinary bindings no longer expire.
    execution_resume_enabled: bool = True
    claude_execution_wait_seconds: float = Field(default=5.0, ge=0, le=30)
    claude_execution_max_waiters: int = Field(default=8, ge=0, le=64)
    max_workers_per_user: int = 10
    max_ws_per_key_worker: int = 10
    max_ws_per_worker: int = 40
    ws_idle_ttl_seconds: float = 600.0
    ws_acquire_timeout_seconds: float = 30.0
    ws_ping_timeout_seconds: float = 300.0
    database_pool_size: int = 20
    database_max_overflow: int = 30
    database_pool_timeout_seconds: float = 10.0
    # Empty node_id keeps the existing single-node deployment compatible.
    node_id: str = ""
    node_gateway_url: str = ""
    node_manager_url: str = ""
    node_timeout_seconds: int = 30
    bootstrap_worker: bool = True

    @model_validator(mode="after")
    def cluster_configuration(self):
        if self.node_id:
            from urllib.parse import urlsplit
            for url in (self.node_gateway_url, self.node_manager_url):
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
                    raise ValueError("Cluster node URLs must be explicit HTTP(S) origins")
            if self.bootstrap_worker:
                raise ValueError("Cluster nodes must disable bootstrap_worker; assign existing Workers explicitly")
            if self.node_timeout_seconds < 15:
                raise ValueError("node_timeout_seconds must be at least 15")
        return self

    @field_validator("admin_cookie_secure", mode="before")
    @classmethod
    def optional_cookie_secure(cls, value):
        # A blank or "auto" value means detect, matching an unset variable.
        return None if isinstance(value, str) and value.strip().lower() in {"", "auto"} else value

    @field_validator("admin_username")
    @classmethod
    def normalize_admin_username(cls, value):
        from .usernames import username_prefix
        return username_prefix(value)

    def public_models(self) -> list[str]:
        configured = [model.strip() for model in self.allowed_models.split(",") if model.strip()]
        return list(dict.fromkeys(configured))

    def provider_map(self) -> dict[str, str]:
        result = {}
        for entry in self.model_providers.split(","):
            model, sep, provider = entry.strip().partition(":")
            if sep and model and provider in {"codex", "gemini", "claude"}:
                result[model] = provider
            elif entry.strip():
                raise ValueError("Invalid model_providers entry")
        return result

    def model_alias_map(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for item in self.model_aliases.split(","):
            public, separator, upstream = item.strip().partition(":")
            if separator and public and upstream:
                aliases[public] = upstream
        return aliases

MIN_SECRET_LENGTH = 16
PLACEHOLDER_MARKERS = ("change-me", "change-this", "changeme")


def weak_secret(value: str) -> bool:
    """Defaults, .env.example placeholders and short values are never production secrets."""
    lowered = value.strip().lower()
    return len(lowered) < MIN_SECRET_LENGTH or any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def insecure_secrets(settings: Settings) -> list[str]:
    """Names of shared secrets that must be replaced before serving real traffic.

    The mock backend stays usable for local development; any real backend or
    cluster node refuses to start with a guessable secret.
    """
    if settings.backend == "mock" and not settings.node_id:
        return []
    secrets = {"CODEX_GATEWAY_KEY_PEPPER": settings.key_pepper,
               "CODEX_GATEWAY_APP_SERVER_TOKEN": settings.app_server_token,
               "CODEX_GATEWAY_MANAGER_TOKEN": settings.manager_token}
    return [name for name, value in secrets.items() if weak_secret(value.get_secret_value())]


@lru_cache
def get_settings() -> Settings:
    return Settings()
