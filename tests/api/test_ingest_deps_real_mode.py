"""build_ingest_deps 운영 분기(배포 전 점검 fix, 2026-06-11) 회귀 테스트.

작성자 : 최태성
담당 영역 : ingestion

종전 결함: ``use_real_adapters=True`` 에서도 run_crawl=run_poc_ingestion(전부 Fake store) /
run_delta=변경 0건 / completion=Noop 으로 고정되어, 실 Confluence 크롤 결과가 인메모리
fake 에 적재된 뒤 소멸했다(실 Qdrant 미적재 무음 결함). 본 테스트는 토글이 운영 wiring
(실 crawl/delta 러너 + 실 completion publisher)을 실제로 선택하는지 계약을 고정한다.

실 인프라(Mongo/RabbitMQ/Qdrant) 연결은 기존 컨벤션(tests/ingestion/test_bootstrap.py)대로
monkeypatch 로 치환한다 — 여기서 검증하는 것은 **조립 분기 계약**이다.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

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
    settings = Settings(_env_file=None, use_real_adapters=True, source_type="atlassian")

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


def test_poc_mode_keeps_credential_lookup_none_when_not_configured(monkeypatch: Any) -> None:
    monkeypatch.setattr(api_deps, "build_soft_delete_store", lambda settings: FakeQdrantPoolStore())
    settings = Settings(
        use_real_adapters=False,
        source_type="json_fixture",
        internal_auth_server_base_url="",
        internal_api_key=SecretStr(""),
    )

    deps = build_ingest_deps(settings)

    assert deps.credential_lookup is None


def test_poc_mode_configures_credential_lookup_when_internal_config_present(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(api_deps, "build_soft_delete_store", lambda settings: FakeQdrantPoolStore())
    # 내부 auth-server base/key 가 있으면 atlassian 모드에서 credential_lookup 을 구성한다.
    monkeypatch.setattr(
        api_deps,
        "build_internal_credential_lookup",
        lambda settings: (
            lambda admin_user_id: (
                f"resolved-{admin_user_id}",
                "resolved-cloud",
            )
        ),
    )
    # build_source_adapter 는 settings bootstrap 시점에서만 사용된다(credential_lookup 구성만 검증).
    monkeypatch.setattr(api_deps, "build_source_adapter", lambda settings: object())

    settings = Settings(
        use_real_adapters=False,
        source_type="atlassian",
        internal_auth_server_base_url="http://auth-server:8080",
        internal_api_key=SecretStr("secret"),
    )

    deps = build_ingest_deps(settings)

    assert deps.credential_lookup is not None
    assert deps.credential_lookup("admin-1") == ("resolved-admin-1", "resolved-cloud")


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


def test_real_crawl_runner_seeds_delta_snapshot_after_successful_full_crawl(
    monkeypatch: Any,
    tmp_path,
) -> None:
    from app.ingestion.crawler import CrawlRequest, CrawlResult
    from app.ingestion.sync import DeltaSnapshotSeedResult

    class _FakeJobsRepo:
        @classmethod
        def from_settings(cls, settings: Settings) -> _FakeJobsRepo:
            return cls()

    class _FakeConnection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    import app.ingestion.crawler as crawler_mod
    import app.storage.jobs as jobs_mod

    connection = _FakeConnection()
    captured: dict[str, Any] = {}

    def _fake_run_full_crawl(*args: Any, **kwargs: Any) -> CrawlResult:
        captured["run_full_crawl_kwargs"] = kwargs
        return CrawlResult(space_key="", pages_collected=12)

    def _fake_seed(request, *, minimum_collected_pages: int | None = None):
        captured["seed_request"] = request
        captured["minimum_collected_pages"] = minimum_collected_pages
        return DeltaSnapshotSeedResult(seeded=True, pages_seeded=12)

    monkeypatch.setattr(jobs_mod, "MongoIngestionJobsRepository", _FakeJobsRepo)
    monkeypatch.setattr(bootstrap, "open_rabbitmq_channel", lambda settings: (connection, object()))
    monkeypatch.setattr(bootstrap, "build_request_source_adapter", lambda *args, **kwargs: object())
    monkeypatch.setattr(crawler_mod, "run_full_crawl", _fake_run_full_crawl)
    monkeypatch.setattr(bootstrap, "seed_delta_snapshot_from_current_metadata", _fake_seed)

    settings = Settings(
        _env_file=None,
        use_real_adapters=True,
        source_type="atlassian",
        data_sync_previous_snapshot=str(tmp_path / "latest_snapshot.json"),
        atlassian_use_admin_key=True,
        atlassian_cloud_id="settings-cloud",
        atlassian_site_url="https://lina.atlassian.net",
        atlassian_admin_email="admin@example.com",
        atlassian_admin_api_token=SecretStr("admin-api-token"),
    )
    runner = bootstrap.build_real_crawl_runner(
        settings, fallback_source=None, raw_store=FakeRawPageStore()
    )

    result = runner(CrawlRequest(access_token="oauth-token", cloud_id="runtime-cloud"))

    seed_request = captured["seed_request"]
    assert result.pages_collected == 12
    assert captured["minimum_collected_pages"] == 12
    assert seed_request.previous_snapshot_path == str(tmp_path / "latest_snapshot.json")
    assert seed_request.cloud_id == "runtime-cloud"
    assert seed_request.use_admin_key is True
    assert seed_request.site_url == "https://lina.atlassian.net"
    assert seed_request.admin_email == "admin@example.com"
    assert seed_request.admin_api_token == "admin-api-token"
    assert connection.closed is True


def test_real_crawl_runner_skips_delta_snapshot_seed_on_partial_failure(
    monkeypatch: Any,
    tmp_path,
) -> None:
    from app.ingestion.crawler import CrawlRequest, CrawlResult

    class _FakeJobsRepo:
        @classmethod
        def from_settings(cls, settings: Settings) -> _FakeJobsRepo:
            return cls()

    class _FakeConnection:
        def close(self) -> None:
            return None

    import app.ingestion.crawler as crawler_mod
    import app.storage.jobs as jobs_mod

    seed_calls: list[object] = []

    monkeypatch.setattr(jobs_mod, "MongoIngestionJobsRepository", _FakeJobsRepo)
    monkeypatch.setattr(
        bootstrap,
        "open_rabbitmq_channel",
        lambda settings: (_FakeConnection(), object()),
    )
    monkeypatch.setattr(bootstrap, "build_request_source_adapter", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        crawler_mod,
        "run_full_crawl",
        lambda *args, **kwargs: CrawlResult(
            space_key="",
            pages_collected=11,
            failed_page_ids=["page-missing"],
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "seed_delta_snapshot_from_current_metadata",
        lambda *args, **kwargs: seed_calls.append(args),
    )

    settings = Settings(
        _env_file=None,
        use_real_adapters=True,
        source_type="atlassian",
        data_sync_previous_snapshot=str(tmp_path / "latest_snapshot.json"),
    )
    runner = bootstrap.build_real_crawl_runner(
        settings, fallback_source=None, raw_store=FakeRawPageStore()
    )

    result = runner(CrawlRequest(access_token="oauth-token", cloud_id="runtime-cloud"))

    assert result.failed_page_ids == ["page-missing"]
    assert seed_calls == []
