from functools import lru_cache
from typing import Literal
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CODEX_GATEWAY_", env_file=".env")
    database_url: str = "postgresql+asyncpg://codex:codex@localhost:5432/codex_gateway"
    key_pepper: SecretStr = SecretStr("development-only-change-me")
    backend: Literal["mock", "app_server"] = "mock"
    dev_api_key: SecretStr | None = SecretStr("cag_dev_local")
    admin_password: SecretStr = SecretStr("development-admin-change-me")
    auto_create_schema: bool = True
    model_name: str = "codex"
    upstream_model: str = "gpt-6-sol"
    allowed_models: str = "gpt-6-sol,gpt-6-astra,gpt-6-luna,gpt-5.6-sol,gpt-5.6-terra,gpt-5.6-luna,gpt-5.6"
    app_server_url: str = "ws://worker-1:4500"
    app_server_token: SecretStr = SecretStr("development-worker-token-change-me")
    app_server_timeout_seconds: float = 300.0
    workspace_host_root: str = "/worker-workspaces/worker-1"
    workspace_worker_root: str = "/workspace"
    manager_url: str = "http://worker-manager:4600"
    manager_token: SecretStr = SecretStr("development-manager-token-change-me")
    max_request_bytes: int = 1_048_576

    def public_models(self) -> list[str]:
        configured = [model.strip() for model in self.allowed_models.split(",") if model.strip()]
        return list(dict.fromkeys([self.model_name, *configured]))

@lru_cache
def get_settings() -> Settings:
    return Settings()
