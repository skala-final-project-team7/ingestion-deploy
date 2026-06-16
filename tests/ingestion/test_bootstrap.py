"""Ingestion 부트스트랩(composition root) 단위 테스트 — PoC 모드 조립 검증.

작성자 : 최태성
담당 영역 : ingestion

실 어댑터 모드는 외부 인프라(E5/Qdrant/Mongo/OpenAI) 의존이라 통합 환경에서 검증하고,
여기서는 PoC 모드(전부 Fake) 조립과 raw_store 공유 선택 로직만 검증한다.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.ingestion.bootstrap import (
    build_auth_server_requester,
    build_internal_credential_lookup,
    build_chunking_worker_deps,
    build_document_analyzer,
    build_raw_page_store,
)
from app.storage.jobs import FakeIngestionJobsRepository
from app.storage.mongo_cache import FakeEmbeddingCache
from app.storage.qdrant_fake import FakeQdrantPoolStore
from app.storage.raw_store import FakeRawPageStore


def _poc_settings() -> Settings:
    return Settings(use_real_adapters=False)


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeAuthServerClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, object] | None, dict[str, str] | None]] = []

    def get(
        self,
        path: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        self.calls.append((path, params, headers))
        return self._response


def test_build_auth_server_requester_includes_internal_key_for_internal_path() -> None:
    settings = Settings(
        internal_api_key="super-secret",
        internal_auth_server_base_url="http://auth-server",
        internal_auth_server_admin_credential_path="/internal/auth/admin-confluence-credential",
    )
    fake_response = _FakeResponse({"accessToken": "at", "cloudId": "cid", "siteUrl": "https://x.atlassian.net"})
    client = _FakeAuthServerClient(fake_response)
    request = build_auth_server_requester(settings, client=client)

    response = request("/internal/auth/admin-confluence-credential", {"adminUserId": "712020:91b5"})

    assert response is fake_response
    assert len(client.calls) == 1
    path, params, headers = client.calls[0]
    assert path == "/internal/auth/admin-confluence-credential"
    assert params == {"adminUserId": "712020:91b5"}
    assert headers == {"X-Internal-Api-Key": "super-secret"}


def test_build_auth_server_requester_skips_internal_key_for_public_path() -> None:
    settings = Settings(
        internal_api_key="super-secret",
        internal_auth_server_base_url="http://auth-server",
    )
    fake_response = _FakeResponse({"ok": True})
    client = _FakeAuthServerClient(fake_response)
    request = build_auth_server_requester(settings, client=client)

    request("/api/v1/public/ping", {})

    assert len(client.calls) == 1
    _, _, headers = client.calls[0]
    assert headers is None


def test_build_internal_credential_lookup_returns_access_token_and_cloud_id() -> None:
    settings = Settings(
        internal_auth_server_base_url="http://auth-server",
        internal_auth_server_admin_credential_path="/internal/auth/admin-confluence-credential",
    )
    requests: list[tuple[str, dict[str, object] | None]] = []

    def _fake_request(
        path: str,
        params: dict[str, object] | None,
    ) -> _FakeResponse:
        requests.append((path, params))
        return _FakeResponse(
            {
                "accessToken": "resolved-token",
                "cloudId": "resolved-cloud",
                "siteUrl": "https://x.atlassian.net",
                "expiresAt": "2026-06-16T00:00:00+09:00",
            }
        )

    lookup = build_internal_credential_lookup(settings, request=_fake_request)
    access_token, cloud_id = lookup("712020:91b5")

    assert access_token == "resolved-token"
    assert cloud_id == "resolved-cloud"
    assert requests == [("/internal/auth/admin-confluence-credential", {"adminUserId": "712020:91b5"})]


def test_build_raw_page_store_poc_returns_fake() -> None:
    assert isinstance(build_raw_page_store(_poc_settings()), FakeRawPageStore)


def test_build_document_analyzer_poc_returns_none() -> None:
    # PoC 는 LLM 비용 0 — chunk_page 라벨 폴백을 사용한다.
    assert build_document_analyzer(_poc_settings()) is None


def test_build_chunking_worker_deps_poc_wires_fakes() -> None:
    deps = build_chunking_worker_deps(_poc_settings())

    assert isinstance(deps.raw_store, FakeRawPageStore)
    assert isinstance(deps.store, FakeQdrantPoolStore)
    assert isinstance(deps.cache, FakeEmbeddingCache)
    assert isinstance(deps.jobs, FakeIngestionJobsRepository)
    assert deps.doc_type_resolver is None


def test_build_chunking_worker_deps_shares_provided_raw_store() -> None:
    shared = FakeRawPageStore()

    deps = build_chunking_worker_deps(_poc_settings(), raw_store=shared)

    # crawl 과 worker 가 같은 raw_store 인스턴스를 공유하도록 주입 가능(in-process PoC).
    assert deps.raw_store is shared


def test_build_chunking_worker_deps_real_threads_embedder_dimension(monkeypatch) -> None:
    pytest.importorskip("sentence_transformers", reason="embedding optional dependency is not installed")
    pytest.importorskip("torch", reason="sentence-transformers runtime dependency missing")

    # 회귀(B): 실 어댑터 모드에서 QdrantPoolStore.from_settings 가 임베더의 '실제' 차원을
    # 전달받아야 컬렉션 차원과 벡터 차원이 일치한다(비-기본 모델 시 mismatch 방지).
    # 이 파일은 원칙적으로 실 어댑터 경로를 통합 환경에서 검증하지만, 본 케이스는 무거운
    # 인프라 없이 '차원 전달 계약'만 검증하려고 실 의존성을 모두 fake 로 치환한다.
    import app.ingestion.bootstrap as bootstrap
    import app.ingestion.embedder.dense as dense_mod
    import app.ingestion.embedder.sparse as sparse_mod
    import app.storage.jobs as jobs_mod
    import app.storage.mongo_cache as cache_mod
    import app.storage.qdrant_client as qdrant_mod

    class _FakeE5:
        def __init__(self, *args, **kwargs) -> None:
            self.dimension = 768  # e5-large(1024) 가 아닌 값으로 threading 을 입증

    captured: dict[str, int] = {}

    class _FakeStore:
        @classmethod
        def from_settings(cls, settings, *, dense_dimension: int = 1024) -> _FakeStore:
            captured["dense_dimension"] = dense_dimension
            return cls()

    class _FakeFromSettings:
        @classmethod
        def from_settings(cls, settings) -> _FakeFromSettings:
            return cls()

    monkeypatch.setattr(dense_mod, "E5DenseEmbedder", _FakeE5)
    monkeypatch.setattr(sparse_mod, "BM25SparseEmbedder", lambda *args, **kwargs: object())
    monkeypatch.setattr(qdrant_mod, "QdrantPoolStore", _FakeStore)
    monkeypatch.setattr(cache_mod, "MongoEmbeddingCache", _FakeFromSettings)
    monkeypatch.setattr(jobs_mod, "MongoIngestionJobsRepository", _FakeFromSettings)
    monkeypatch.setattr(bootstrap, "build_document_analyzer", lambda settings: None)

    deps = build_chunking_worker_deps(
        Settings(use_real_adapters=True), raw_store=FakeRawPageStore()
    )

    # 기본값 1024 가 아니라 임베더가 보고한 768 이 전달되어야 한다.
    assert captured["dense_dimension"] == 768
    assert deps.dense_embedder.dimension == 768


def test_default_completion_queue_settings() -> None:
    settings = Settings()

    assert settings.ingest_completion_routing_key == "lina.admin.ingest.completion"
    assert settings.ingest_completion_queue == "lina.admin.ingest.completion"
    assert settings.ingest_completion_dlq == "lina.admin.ingest.completion.dlq"


def test_override_completion_queue_settings() -> None:
    settings = Settings(
        ingest_completion_routing_key="completion.key",
        ingest_completion_queue="completion.queue",
        ingest_completion_dlq="completion.queue.dlq",
    )

    assert settings.ingest_completion_routing_key == "completion.key"
    assert settings.ingest_completion_queue == "completion.queue"
    assert settings.ingest_completion_dlq == "completion.queue.dlq"


def test_default_ingest_job_queue_settings() -> None:
    settings = Settings()

    assert settings.ingest_job_exchange == "lina.admin.ingest"
    assert settings.ingest_job_routing_key == "admin.ingest.requested"
    assert settings.ingest_job_queue == "lina.data-ingestion.ingest"
    assert settings.ingest_job_dlq == "lina.data-ingestion.ingest.dlq"


def test_override_ingest_job_queue_settings() -> None:
    settings = Settings(
        ingest_job_exchange="exchange.test",
        ingest_job_routing_key="route.test",
        ingest_job_queue="queue.test",
        ingest_job_dlq="queue.test.dlq",
    )

    assert settings.ingest_job_exchange == "exchange.test"
    assert settings.ingest_job_routing_key == "route.test"
    assert settings.ingest_job_queue == "queue.test"
    assert settings.ingest_job_dlq == "queue.test.dlq"
