"""Ingestion 의존성 부트스트랩(composition root) — Settings 기반 어댑터 조립 [Pipeline].

--------------------------------------------------
작성자 : 최태성
작성목적 : Worker·crawl 이 사용하는 외부 의존성(raw_store / 임베더 / Qdrant / cache / jobs /
          문서 분석기)을 ``Settings.use_real_adapters`` 토글에 따라 PoC(전부 Fake) 또는 실
          어댑터로 조립한다(config.py 의 use_real_adapters 패턴 재사용). 실 어댑터는 함수 내
          지연 import 로 무거운 의존(torch/qdrant/openai)을 실행 시점으로 미룬다. RabbitMQ
          연결을 소유한 실행 loop·CLI 엔트리포인트는 인프라 의존이라 후속(featureI-7c)으로
          분리한다 — 본 모듈은 **데이터 의존성 조립**만 책임진다.
작성일 : 2026-05-26 (featureI-7b)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-26, 최초 작성, featureI-7b — build_raw_page_store / build_document_analyzer /
    build_chunking_worker_deps (PoC vs real).
  - 2026-06-09, FR-002 — build_attachment_downloader 추가(atlassian 소스 시 HttpAttachmentDownloader
    주입; fixture 는 None). build_chunking_worker_deps 실 branch 에 배선.
  - 2026-06-10, 코드 리뷰 재점검(A11·A16) — (1) build_attachment_downloader 에 호스트
    allowlist(atlassian_api_base_url) + file:// prefix(samples_dir) 검증 배선. (2) infra
    진입점용 운영 wiring 헬퍼 추가: build_ingest_completion_publisher(라우팅 키 설정 소비 —
    dead config 해소) / build_delta_runner(run_delta_sync + full crawl 과 동일 acl_provider).
    RabbitMQ 연결 소유는 여전히 infra 책임(featureI-7c) — 채널/QueuePublisher 만 받으면
    한 줄로 배선되도록 조립부를 제공한다.
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - 실 어댑터 모드는 sentence-transformers/fastembed/qdrant-client/pymongo/sqlalchemy/openai 필요
    (지연 import). PoC 모드는 외부 의존성 0.
--------------------------------------------------
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from app.config import Settings, get_settings
from app.ingestion.embedder.base import FakeDenseEmbedder, FakeSparseEmbedder
from app.ingestion.sync import DeltaSyncRequest, DeltaSyncResult
from app.ingestion.workers.chunking_worker import ChunkingWorkerDeps
from app.ingestion.workers.publisher import QueuePublisher
from app.storage.jobs import FakeIngestionJobsRepository
from app.storage.mongo_cache import FakeEmbeddingCache
from app.storage.qdrant_fake import FakeQdrantPoolStore
from app.storage.raw_store import FakeRawPageStore, RawPageStore

if TYPE_CHECKING:
    from app.api.ingest_completion import IngestCompletionPublisher
    from app.ingestion.attachment_downloader import AttachmentDownloader
    from app.ingestion.document_analyzer import DocumentAnalyzer
    from app.ingestion.soft_delete import SoftDeleteStore


def build_raw_page_store(settings: Settings | None = None) -> RawPageStore:
    """``raw_pages`` 스토어를 조립한다 — PoC: in-memory / 실: MongoRawPageStore."""
    resolved = settings or get_settings()
    if not resolved.use_real_adapters:
        return FakeRawPageStore()
    from app.storage.raw_store import MongoRawPageStore

    return MongoRawPageStore.from_settings(resolved)


def build_document_analyzer(settings: Settings | None = None) -> DocumentAnalyzer | None:
    """문서 분석기[Agent]를 조립한다.

    PoC 모드는 ``None`` 을 반환해 chunk_page 의 라벨 휴리스틱 폴백을 쓰고(LLM 비용 0),
    실 모드는 GPT-4o-mini 분류기 + MySQL 캐시로 구성한다.
    """
    resolved = settings or get_settings()
    if not resolved.use_real_adapters:
        return None
    from app.ingestion.document_analyzer import DocumentAnalyzer, OpenAIDocTypeClassifier
    from app.storage.space_doc_type_cache import MySQLSpaceDocTypeCache

    classifier = OpenAIDocTypeClassifier(
        api_key=resolved.openai_api_key.get_secret_value(),
        model=resolved.llm_aux_model,
    )
    cache = MySQLSpaceDocTypeCache.from_settings(resolved)
    return DocumentAnalyzer(classifier=classifier, cache=cache)


def build_chunking_worker_deps(
    settings: Settings | None = None,
    *,
    raw_store: RawPageStore | None = None,
) -> ChunkingWorkerDeps:
    """Chunking+Embedding Worker 의존성을 조립한다(PoC: Fake / 실: E5+BM25+Qdrant+Mongo).

    Args:
        settings: 환경 설정. None 이면 프로세스 단일 인스턴스.
        raw_store: crawl 과 공용으로 쓸 raw_store(in-process PoC 공유용). None 이면
            ``build_raw_page_store`` 로 생성한다(운영은 프로세스별 Mongo 연결 → DB 공유).
    """
    resolved = settings or get_settings()
    store = raw_store or build_raw_page_store(resolved)
    if not resolved.use_real_adapters:
        return ChunkingWorkerDeps(
            raw_store=store,
            dense_embedder=FakeDenseEmbedder(),
            sparse_embedder=FakeSparseEmbedder(),
            store=FakeQdrantPoolStore(),
            cache=FakeEmbeddingCache(),
            jobs=FakeIngestionJobsRepository(),
            doc_type_resolver=None,
        )
    from app.ingestion.embedder.dense import E5DenseEmbedder
    from app.ingestion.embedder.sparse import BM25SparseEmbedder
    from app.storage.jobs import MongoIngestionJobsRepository
    from app.storage.mongo_cache import MongoEmbeddingCache
    from app.storage.qdrant_client import QdrantPoolStore

    # Qdrant 컬렉션은 임베더의 실제 차원으로 부트스트랩해야 한다. from_settings 의
    # dense_dimension 기본값(1024)에 의존하면, e5-large(1024) 가 아닌 모델을
    # RAG_DENSE_EMBEDDING_MODEL 로 설정했을 때 컬렉션 차원과 벡터 차원이 어긋나
    # upsert 가 실패한다. 따라서 임베더가 보고한 dimension 을 명시 전달한다
    # (rag build_real_deps 패턴 정합).
    dense_embedder = E5DenseEmbedder(model_name=resolved.dense_embedding_model)
    return ChunkingWorkerDeps(
        raw_store=store,
        dense_embedder=dense_embedder,
        sparse_embedder=BM25SparseEmbedder(),
        store=QdrantPoolStore.from_settings(resolved, dense_dimension=dense_embedder.dimension),
        cache=MongoEmbeddingCache.from_settings(resolved),
        jobs=MongoIngestionJobsRepository.from_settings(resolved),
        doc_type_resolver=build_document_analyzer(resolved),
        attachment_downloader=build_attachment_downloader(resolved),
    )


def build_attachment_downloader(settings: Settings | None = None) -> AttachmentDownloader | None:
    """첨부 다운로더를 조립한다 — atlassian 소스면 HttpAttachmentDownloader, 아니면 None.

    fixture 소스(json_fixture)는 ``local_path`` 를 이미 채우므로 다운로더가 불필요(None).
    atlassian 소스는 download_url 만 제공하므로, 기존 Confluence 클라이언트와 동일한 인증 헤더
    (Bearer access_token + 선택 Admin Key)를 구성한 httpx client 로 HttpAttachmentDownloader 를
    만든다. credential SOURCE 는 현 codebase 패턴(settings)을 따른다 — v2.5 adminUserId →
    auth-server 조회로의 이전은 모든 Confluence client 공통 후속이다.
    """
    resolved = settings or get_settings()
    if resolved.source_type != "atlassian":
        return None
    from urllib.parse import urlparse

    import httpx

    from app.ingestion.attachment_downloader import HttpAttachmentDownloader

    headers = {"Authorization": f"Bearer {resolved.atlassian_access_token.get_secret_value()}"}
    if resolved.atlassian_use_admin_key:
        headers["Atl-Confluence-With-Admin-Key"] = "true"
    # A11 — 자격증명이 실린 다운로드는 Atlassian API 호스트로만 허용하고, file:// URI 는
    # 픽스처 디렉터리(samples_dir) 아래로만 제한한다(저장 데이터의 download_url 은 신뢰 경계 밖).
    api_host = urlparse(resolved.atlassian_api_base_url).hostname
    return HttpAttachmentDownloader(
        download_dir=resolved.attachment_download_dir,
        client=httpx.Client(headers=headers),
        allowed_hosts=[api_host] if api_host else None,
        file_uri_allowed_prefix=resolved.samples_dir,
    )


def build_ingest_completion_publisher(
    queue_publisher: QueuePublisher, settings: Settings | None = None
) -> IngestCompletionPublisher:
    """완료 이벤트 publisher 조립(A16) — ``ingest_completion_routing_key`` 설정을 소비한다.

    RabbitMQ 채널/연결 소유는 infra 진입점 책임(featureI-7c)이므로, 구성된
    ``QueuePublisher`` 를 받아 ``QueueIngestCompletionPublisher`` 로 감싸기만 한다::

        publisher = PikaQueuePublisher(channel)
        deps.completion_publisher = build_ingest_completion_publisher(publisher, settings)
    """
    from app.api.ingest_completion import QueueIngestCompletionPublisher

    resolved = settings or get_settings()
    return QueueIngestCompletionPublisher(
        publisher=queue_publisher,
        routing_key=resolved.ingest_completion_routing_key,
    )


def build_delta_runner(
    settings: Settings | None = None,
    *,
    raw_store: RawPageStore | None = None,
    queue_publisher: QueuePublisher | None = None,
) -> Callable[[DeltaSyncRequest], DeltaSyncResult]:
    """운영 delta 러너 조립(A16) — ``run_delta_sync`` 에 full crawl 과 동일 acl_provider 를 배선.

    chunking 재투입 publisher(RabbitMQ)는 infra 가 구성해 전달한다(미전달 시 PoC
    ``FakeQueuePublisher`` — 발행 검증/local 용). 반환 callable 은 ``IngestDeps.run_delta``
    에 그대로 주입한다(코드 리뷰 A3·A16)::

        deps.run_delta = build_delta_runner(settings, raw_store=store, queue_publisher=pub)
    """
    from app.adapters.atlassian import build_restriction_acl_provider
    from app.ingestion.sync import run_delta_sync
    from app.ingestion.workers.publisher import FakeQueuePublisher

    resolved = settings or get_settings()
    store = raw_store or build_raw_page_store(resolved)
    publisher = queue_publisher or FakeQueuePublisher()
    acl_provider = build_restriction_acl_provider(resolved)

    def _run_delta(request: DeltaSyncRequest) -> DeltaSyncResult:
        return run_delta_sync(
            request,
            raw_store=store,
            publisher=publisher,
            acl_provider=acl_provider,
        )

    return _run_delta


def build_soft_delete_store(settings: Settings | None = None) -> SoftDeleteStore:
    """삭제 트리거(Sync Worker)가 사용할 soft-delete store 를 조립한다(featureI-5b).

    soft-delete 는 기존 Point 의 payload ``is_deleted`` 만 set(set_payload)하므로 임베딩
    차원과 무관하다(컬렉션은 ingest 경로가 생성). 따라서 실 모드는 임베더 로딩 없이
    ``from_settings`` 기본값으로 운영 Qdrant 에 연결한다(이미 존재하는 컬렉션 → create no-op).

    - **PoC** (``use_real_adapters=False``): in-memory ``FakeQdrantPoolStore``. 단, HTTP
      ingest 합성 파이프라인(``run_poc_ingestion``)은 자체 내부 Fake store 를 쓰므로 본
      store 와 분리돼 있어 webhook soft-delete 가 no-op 일 수 있다(PoC 데모 한계 — 문서화).
    - **실** (``use_real_adapters=True``): 운영 ``QdrantPoolStore`` 로 연결해 공유 Qdrant 의
      Point 를 soft-delete 한다(ingest 와 동일 컬렉션).

    주기 Trash 동기화·RabbitMQ 실행 loop 는 인프라 의존 후속(featureI-7c)이며, 본 함수는
    **store 조립**만 책임진다.
    """
    resolved = settings or get_settings()
    if not resolved.use_real_adapters:
        return FakeQdrantPoolStore()
    from app.storage.qdrant_client import QdrantPoolStore

    return QdrantPoolStore.from_settings(resolved)
