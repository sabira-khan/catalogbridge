from __future__ import annotations

from collections.abc import Callable

import pytest
from app.config import Settings, get_settings

SettingsFactory = Callable[..., Settings]

ENV_VARS = (
    "NEBIUS_API_KEY",
    "NEBIUS_BASE_URL",
    "LLM_MODEL",
    "LLM_MODEL_FAST",
    "EMBED_MODEL",
    "HTTP_TIMEOUT",
    "HTTP_USER_AGENT",
    "LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
def settings_factory(monkeypatch: pytest.MonkeyPatch) -> SettingsFactory:
    def build(**overrides: str) -> Settings:
        for name in ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        for key, value in overrides.items():
            monkeypatch.setenv(key.upper(), value)
        return Settings(_env_file=None)

    return build
