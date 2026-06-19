"""Settings 기본값/환경변수 매핑 테스트."""

from __future__ import annotations

from app.config import Settings


def test_otel_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.otel_enabled is False
    assert settings.otel_service_name == "ingestion-api"
    assert (
        settings.otel_exporter_otlp_endpoint
        == "http://otel-collector.skala3-finalproj-class2-team7.svc.cluster.local:4317"
    )
    assert settings.otel_environment == "dev"


def test_otel_env_override(monkeypatch) -> None:
    monkeypatch.setenv("RAG_OTEL_ENABLED", "true")
    monkeypatch.setenv("RAG_OTEL_SERVICE_NAME", "ingestion-worker")
    monkeypatch.setenv("RAG_OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("RAG_OTEL_ENVIRONMENT", "prod")

    settings = Settings(_env_file=None)

    assert settings.otel_enabled is True
    assert settings.otel_service_name == "ingestion-worker"
    assert settings.otel_exporter_otlp_endpoint == "http://collector:4317"
    assert settings.otel_environment == "prod"
