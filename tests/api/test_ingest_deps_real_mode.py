"""build_ingest_deps 운영 분기(배포 전 점검 fix, 2026-06-11) 회귀 테스트.

종전 결함: ``use_real_adapters=True`` 에서도 run_crawl=run_poc_ingestion(전부 Fake store) /
run_delta=변경 0건 / completion=Noop 으로 고정되어, 실 Confluence 크롤 결과가 인메모리
fake 에 적재된 뒤 소멸했다(실 Qdrant 미적재 무음 결함). 본 테스트는 토글이 운영 wiring
(실 crawl/delta 러너 + 실 completion publisher)을 실제로 선택하는지 계약을 고정한다.

실 인프라(Mongo/RabbitMQ/Qdrant) 연결은 기존 컨벤션(tests/ingestion/test_bootstrap.py)대로
monkeypatch 로 치환한다 — 여기서 검증하는 것은 **조립 분기 계약**이다.
"""

from __future__ import annotations

from typing import Any

import app.api.deps as api_deps
import app.ingestion.bootstrap as bootstrap
from app.api.deps import _poc_empty_delta, build_ingest_deps
from app.api.ingest_completion import NoopIngestCompletionPublisher
from app.config import Settings
from app.storage.qdrant_fake import FakeQdrantPoolStore
from app.storage.raw_store import FakeRawPageStore


def _patch_real_infra(monkeypatch: Any) -> dict[str, object]:
    """운영 분기가 소비하는 인프라 빌더를 전부 fake 로 치환하고 호출 기록을 남긴다."""
    calls: dict[str, object] = {}

    def _fake_crawl_runner(settings: Settings, **kwargs: Any) -> object:
        calls["crawl_runner"] = kwargs
        return lambda request: None

    def _fake_delta_runner(settings: Settings, **kwargs: Any) -> object:
        calls["delta_runner"] = kwargs
        return lambda request: None

    class _RealCompletionPublisher:
        def publish_completion(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
            raise AssertionError("테스트에서 발행하지 않는다")

    monkeypatch.setattr(bootstrap, "build_real_crawl_runner", _fake_crawl_runner)
    monkeypatch.setattr(bootstrap, "build_real_delta_runner", _fake_delta_runner)
    monkeypatch.setattr(
        bootstrap, "build_real_completion_publisher", lambda settings: _RealCompletionPublisher()
    )
    monkeypatch.setattr(bootstrap, "build_raw_page_store", lambda settings: FakeRawPageStore())
    # soft-delete store 는 api_deps 모듈 네임스페이스로 import 되어 있다 — 실 Qdrant 차단.
    monkeypatch.setattr(api_deps, "build_soft_delete_store", lambda settings: FakeQdrantPoolStore())
    calls["completion_type"] = _RealCompletionPublisher
    return calls


def test_real_mode_wires_real_runners_and_publisher(monkeypatch: Any) -> None:
    calls = _patch_real_infra(monkeypatch)
    settings = Settings(use_real_adapters=True, source_type="json_fixture")

    deps = build_ingest_deps(settings)

    # 운영 분기 — PoC 기본값(Noop/변경 0건)이 아니어야 한다.
    assert not isinstance(deps.completion_publisher, NoopIngestCompletionPublisher)
    assert isinstance(deps.completion_publisher, calls["completion_type"])  # type: ignore[arg-type]
    assert deps.run_delta is not _poc_empty_delta
    assert "crawl_runner" in calls and "delta_runner" in calls
    # crawl·delta 가 같은 raw_store 인스턴스를 공유한다(Mongo 연결 1개).
    crawl_kwargs: dict[str, Any] = calls["crawl_runner"]  # type: ignore[assignment]
    delta_kwargs: dict[str, Any] = calls["delta_runner"]  # type: ignore[assignment]
    assert crawl_kwargs["raw_store"] is delta_kwargs["raw_store"]
    # 설정 주입 계약(스냅샷 경로·삭제 확인 게이트)은 모드와 무관하게 유지된다.
    assert deps.previous_snapshot_path == settings.data_sync_previous_snapshot
    assert deps.delta_delete_confirm is settings.data_sync_delta_delete_confirm


def test_real_mode_boots_without_atlassian_credentials(monkeypatch: Any) -> None:
    # v2.5.0 — 자격증명은 BFF 가 잡마다 전달한다. Settings 자격증명이 비어 있어도
    # (source_type=atlassian) 부팅은 성공하고, fallback 어댑터만 None 이어야 한다.
    calls = _patch_real_infra(monkeypatch)
    settings = Settings(use_real_adapters=True, source_type="atlassian")

    deps = build_ingest_deps(settings)

    assert deps is not None
    crawl_kwargs: dict[str, Any] = calls["crawl_runner"]  # type: ignore[assignment]
    assert crawl_kwargs["fallback_source"] is None


def test_poc_mode_keeps_safe_defaults(monkeypatch: Any) -> None:
    # PoC 경로 비파괴 — 합성 러너 + Noop publisher + 변경 0건 delta.
    monkeypatch.setattr(api_deps, "build_soft_delete_store", lambda settings: FakeQdrantPoolStore())
    settings = Settings(use_real_adapters=False, source_type="json_fixture")

    deps = build_ingest_deps(settings)

    assert isinstance(deps.completion_publisher, NoopIngestCompletionPublisher)
    assert deps.run_delta is _poc_empty_delta


def test_real_crawl_runner_without_any_adapter_fails_loud(monkeypatch: Any) -> None:
    # 요청에도 Settings 에도 자격증명이 없으면 — 무음 성공이 아니라 명시 실패(RuntimeError)
    # 로 잡이 FAILED 기록되어야 한다.
    import pytest

    from app.ingestion.crawler import CrawlRequest

    class _FakeJobsRepo:
        @classmethod
        def from_settings(cls, settings: Settings) -> _FakeJobsRepo:
            return cls()

    import app.storage.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "MongoIngestionJobsRepository", _FakeJobsRepo)
    settings = Settings(use_real_adapters=True, source_type="atlassian")

    runner = bootstrap.build_real_crawl_runner(
        settings, fallback_source=None, raw_store=FakeRawPageStore()
    )

    with pytest.raises(RuntimeError, match="어댑터를 결정할 수 없다"):
        runner(CrawlRequest())
