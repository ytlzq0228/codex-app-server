from functools import lru_cache
from typing import Literal
from pydantic import SecretStr, field_validator
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
    model_providers: str = ""  # Explicit public-model:provider entries
    model_aliases: str = "gpt-5.6:gpt-5.6-sol"
    app_server_url: str = "ws://worker-1:4500"
    app_server_token: SecretStr = SecretStr("development-worker-token-change-me")
    app_server_timeout_seconds: float = 300.0
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
    max_workers_per_user: int = 10
    max_ws_per_key_worker: int = 10
    max_ws_per_worker: int = 40
    ws_idle_ttl_seconds: float = 600.0
    ws_acquire_timeout_seconds: float = 30.0
    ws_ping_timeout_seconds: float = 300.0
    database_pool_size: int = 20
    database_max_overflow: int = 30
    database_pool_timeout_seconds: float = 10.0

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

@lru_cache
def get_settings() -> Settings:
    return Settings()
