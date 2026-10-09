from __future__ import annotations

from app import __version__
from app.config import get_settings
from app.main import create_app
from fastapi.testclient import TestClient

from tests.conftest import SettingsFactory


def _client(settings_factory: SettingsFactory, **overrides: str) -> TestClient:
    application = create_app()
    settings = settings_factory(**overrides)
    application.dependency_overrides[get_settings] = lambda: settings
    return TestClient(application)


def test_health_reports_configured_models(settings_factory: SettingsFactory) -> None:
    response = _client(settings_factory).get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["llm_model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert body["embed_model"] == "Qwen/Qwen3-Embedding-8B"
    assert body["llm_configured"] is False


def test_health_never_leaks_the_api_key(settings_factory: SettingsFactory) -> None:
    secret = "super-secret-key-value"
    response = _client(settings_factory, nebius_api_key=secret).get("/health")
    assert response.status_code == 200
    assert secret not in response.text
    assert response.json()["llm_configured"] is True
