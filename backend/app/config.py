from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import MissingAPIKeyError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    nebius_api_key: str | None = None
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    llm_model: str = "nvidia/nemotron-3-super-120b-a12b"
    llm_model_fast: str = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
    embed_model: str = "Qwen/Qwen3-Embedding-8B"

    http_timeout: float = 30.0
    http_user_agent: str = "CatalogBridge/0.1 (+https://github.com/sabira-khan/catalogbridge)"
    log_level: str = "INFO"

    llm_temperature: float = 0.0
    llm_max_attempts: int = 4
    llm_max_repairs: int = 1

    database_path: str = "catalogbridge.db"

    connector_page_size: int = 250
    connector_max_pages: int = 100
    connector_page_delay: float = 0.2
    connector_max_attempts: int = 4
    connector_backoff_base: float = 1.0
    connector_backoff_cap: float = 20.0

    @property
    def llm_configured(self) -> bool:
        return bool(self.nebius_api_key)

    def require_api_key(self) -> str:
        if not self.nebius_api_key:
            raise MissingAPIKeyError
        return self.nebius_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
