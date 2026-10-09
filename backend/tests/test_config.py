from __future__ import annotations

import pytest
from app.errors import MissingAPIKeyError

from tests.conftest import SettingsFactory


def test_defaults_point_at_token_factory(settings_factory: SettingsFactory) -> None:
    settings = settings_factory()
    assert settings.nebius_base_url == "https://api.tokenfactory.nebius.com/v1/"
    assert settings.llm_model == "nvidia/nemotron-3-super-120b-a12b"
    assert settings.llm_model_fast == "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
    assert settings.embed_model == "Qwen/Qwen3-Embedding-8B"


def test_env_overrides_are_applied(settings_factory: SettingsFactory) -> None:
    settings = settings_factory(llm_model="nvidia/Nemotron-3-Ultra-550b-a55b", log_level="DEBUG")
    assert settings.llm_model == "nvidia/Nemotron-3-Ultra-550b-a55b"
    assert settings.log_level == "DEBUG"


def test_llm_not_configured_without_key(settings_factory: SettingsFactory) -> None:
    settings = settings_factory()
    assert settings.nebius_api_key is None
    assert settings.llm_configured is False


def test_require_api_key_raises_when_unset(settings_factory: SettingsFactory) -> None:
    settings = settings_factory()
    with pytest.raises(MissingAPIKeyError):
        settings.require_api_key()


def test_require_api_key_returns_key(settings_factory: SettingsFactory) -> None:
    settings = settings_factory(nebius_api_key="test-key")
    assert settings.require_api_key() == "test-key"
    assert settings.llm_configured is True
