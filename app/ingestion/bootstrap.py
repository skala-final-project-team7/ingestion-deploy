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
  - 2026-06-11, 배포 전 점검 fix — 운영 RabbitMQ wiring 을 본 레포가 직접 소유하도록 전환.
    open_rabbitmq_channel(durable 큐 선언) / build_request_source_adapter(요청 자격증명 +
    Settings ACL 구성) / build_real_crawl_runner(실 raw_store+jobs+발행, 잡 단위 연결) /
    build_real_delta_runner / build_real_completion_publisher 추가. 종전에는 API 가
    use_real_adapters=True 에서도 run_poc_ingestion(전부 Fake)으로 고정되어 실 Qdrant 에
    아무것도 적재되지 않았다(무음 결함 — build_ingest_deps 의 운영 분기에서 소비).
  - 2026-06-11, backend-template 동기화(§2-5 siteUrl — api-spec v2.6.2) — delta 러너와
    요청 자격증명 어댑터에 ``settings.atlassian_site_url``(=§2-5 ``siteUrl``) 을 배선해
    full crawl/delta 양 경로의 ``webui_link`` 가 absolute 로 적재되게 한다. atlassian
    소스인데 site_url 미주입이면 출처 링크가 상대경로로 남는다는 WARNING 을 1회 남긴다
    (_warn_if_site_url_missing).
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - 실 어댑터 모드는 sentence-transformers/fastembed/qdrant-client/pymongo/sqlalchemy/openai 필요
    (지연 import). PoC 모드는 외부 의존성 0.
--------------------------------------------------
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

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
    from app.adapters.base import DocumentSourceAdapter
    from app.api.ingest_completion import IngestCompletionPublisher
    from app.ingestion.attachment_downloader import AttachmentDownloader
    from app.ingestion.crawler import CrawlRequest, CrawlResult
    from app.ingestion.document_analyzer import DocumentAnalyzer
    from app.ingestion.soft_delete import SoftDeleteStore

_LOGGER = logging.getLogger(__name__)


def _warn_if_site_url_missing(settings: Settings) -> None:
    """atlassian 소스인데 ``atlassian_site_url`` 미주입이면 WARNING 1회.

    backend api-spec §2-5(2026-06-11): credential lookup 응답 ``siteUrl``
    (=``admin_atlassian_credential.site_url``)은 출처 링크(`_links.webui` 상대경로)를
    absolute 로 정규화해 Qdrant ``webui_link``/RAG ``sources[].url`` 에 적재하기 위한
    값이다. 미주입이면 출처 링크가 상대경로로 남아 FE 출처 카드가 깨진다(무음 결함
    방지 — 기동/조립 시점에 알린다). env: ``RAG_ATLASSIAN_SITE_URL``.
    """
    if settings.source_type.lower() == "atlassian" and not settings.atlassian_site_url:
        _LOGGER.warning(
            "RAG_ATLASSIAN_SITE_URL 이 비어 있다 — webui_link/sources[].url 이 상대경로로 "
            "적재된다. §2-5 credential lookup 의 siteUrl 값(https://{site}.atlassian.net)을 "
            "주입하라(backend docs/api-spec.md §2-5, site_url 단일화 2026-06-11)."
        )


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

    # 자격증명 모델(v2.6.1 정정 → v2.6.2 ML 측 단서로 보존): admin-key 경로는 admin API Token 의
    # Basic 인증 + Admin Key 헤더(다운로드 URL 도 site 도메인), OAuth 경로는 Bearer.
    if resolved.atlassian_use_admin_key:
        from app.adapters.atlassian import build_admin_basic_authorization

        headers = {
            "Authorization": build_admin_basic_authorization(
                resolved.atlassian_admin_email,
                resolved.atlassian_admin_api_token.get_secret_value(),
            ),
            "Atl-Confluence-With-Admin-Key": "true",
        }
    else:
        headers = {"Authorization": f"Bearer {resolved.atlassian_access_token.get_secret_value()}"}
    # A11 — 자격증명이 실린 다운로드는 Atlassian API/site 호스트로만 허용하고, file:// URI 는
    # 픽스처 디렉터리(samples_dir) 아래로만 제한한다(저장 데이터의 download_url 은 신뢰 경계 밖).
    api_host = urlparse(resolved.atlassian_api_base_url).hostname
    site_host = (
        urlparse(resolved.atlassian_site_url).hostname if resolved.atlassian_site_url else None
    )
    allowed_hosts = [host for host in (api_host, site_host) if host]
    return HttpAttachmentDownloader(
        download_dir=resolved.attachment_download_dir,
        client=httpx.Client(headers=headers),
        allowed_hosts=allowed_hosts or None,
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
    _warn_if_site_url_missing(resolved)

    def _run_delta(request: DeltaSyncRequest) -> DeltaSyncResult:
        return run_delta_sync(
            request,
            raw_store=store,
            publisher=publisher,
            acl_provider=acl_provider,
            site_url=resolved.atlassian_site_url,
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


# --- 운영 RabbitMQ wiring (배포 전 점검 fix, 2026-06-11) ---
# 종전에는 "RabbitMQ 연결 소유 = infra 책임" 으로 미뤄 두었으나, HTTP API 가
# use_real_adapters=True 에서도 run_poc_ingestion(전부 Fake store)으로 고정 동작해
# 실 Confluence 를 크롤링하고도 실 Qdrant 에 아무것도 적재되지 않는 무음 결함이 있었다.
# 아래 헬퍼들로 API 가 직접 운영 경로(실 raw_store/Mongo jobs + RabbitMQ 발행)를 조립한다.
# 연결은 **잡 단위로 열고 닫는다** — pika BlockingConnection 은 thread-safe 하지 않고
# BackgroundTasks 가 threadpool 에서 잡을 돌리므로, 잡마다 독립 연결이 가장 안전하다
# (full crawl 은 장시간·저빈도라 연결 비용이 무시 가능).


def open_rabbitmq_channel(settings: Settings | None = None) -> tuple[Any, Any]:
    """RabbitMQ BlockingConnection + channel 을 연다(호출자가 connection.close() 책임).

    발행 대상 큐(``content.chunking`` / completion 라우팅 키)를 durable 로 선언해
    consumer(워커/BFF)가 늦게 떠도 메시지가 유실되지 않게 한다(멱등 — 이미 있으면 no-op).
    publisher 는 default exchange 를 쓰므로 routing_key == queue 이름이다.

    Returns:
        ``(connection, channel)`` 튜플.
    """
    import pika

    from app.ingestion.workers import QUEUE_CHUNKING

    resolved = settings or get_settings()
    connection = pika.BlockingConnection(pika.URLParameters(resolved.rabbitmq_url))
    channel = connection.channel()
    channel.queue_declare(queue=QUEUE_CHUNKING, durable=True)
    channel.queue_declare(queue=resolved.ingest_completion_routing_key, durable=True)
    return connection, channel


def build_request_source_adapter(
    settings: Settings, *, cloud_id: str, access_token: str
) -> DocumentSourceAdapter:
    """요청 주입 자격증명 + Settings 의 ACL/재시도 구성으로 Atlassian 어댑터를 만든다.

    api-spec v2.5.0 흐름(BFF 가 잡마다 access_token/cloud_id 전달)용. ``from_settings``
    와 동일한 acl_provider/딜레이/재시도/Admin Key 구성을 쓰되 자격증명만 요청 값으로
    교체한다 — 종전 ``run_full_crawl(adapter=None)`` 의 bare 생성자 경로는 Settings 의
    restriction ACL 구성이 빠져 운영 ACL 정책과 어긋났다.

    admin-key 경로(``atlassian_use_admin_key=True``)는 v2.6.1 정정(v2.6.2 보존)에 따라 요청 OAuth
    토큰이 아니라 Settings 의 admin API Token(Basic + site URL) 클라이언트를 주입한다 —
    요청 토큰은 vendored config 필수 검증만 채우고 실제 호출에 쓰이지 않는다.
    """
    from app.adapters.atlassian import (
        AtlassianSourceAdapter,
        _build_admin_confluence_client,
        build_restriction_acl_provider,
    )

    client = _build_admin_confluence_client(settings) if settings.atlassian_use_admin_key else None
    _warn_if_site_url_missing(settings)
    return AtlassianSourceAdapter(
        cloud_id=cloud_id,
        access_token=access_token,
        client=client,
        acl_provider=build_restriction_acl_provider(settings),
        request_delay_seconds=settings.atlassian_request_delay_seconds,
        max_retries=settings.atlassian_max_retries,
        timeout_seconds=settings.atlassian_timeout_seconds,
        use_admin_key=settings.atlassian_use_admin_key,
        site_url=settings.atlassian_site_url,
    )


def build_real_crawl_runner(
    settings: Settings,
    *,
    fallback_source: DocumentSourceAdapter | None = None,
    raw_store: RawPageStore | None = None,
) -> Callable[[CrawlRequest], CrawlResult]:
    """운영 full-crawl 러너 — 실 raw_store(Mongo) + Mongo jobs + RabbitMQ 발행.

    ``run_full_crawl`` 로 페이지를 Mongo ``raw_pages`` 에 적재하고 ``content.chunking``
    메시지를 실 RabbitMQ 로 발행한다(소비·색인은 chunking worker —
    ``python -m app.ingestion.workers.chunking_main``).

    어댑터 선택(잡 단위):
      1. 요청에 access_token+cloud_id 가 있으면 요청 자격증명 어댑터(v2.5.0 BFF 흐름).
      2. 없으면 ``fallback_source`` (Settings 기반 — json_fixture 스테이징 포함).
      3. 둘 다 없으면 RuntimeError — 잡이 FAILED 로 기록되고 원인이 로그에 남는다.

    Args:
        settings: 환경 설정.
        fallback_source: 요청 자격증명이 없을 때 쓸 startup 어댑터(없으면 None).
        raw_store: 공유할 raw_store(미주입 시 ``build_raw_page_store`` 로 생성).

    Returns:
        ``CrawlRequest -> CrawlResult`` 러너(IngestDeps.run_crawl 주입용).
    """
    from app.ingestion.crawler import run_full_crawl
    from app.ingestion.workers.publisher import PikaQueuePublisher
    from app.storage.jobs import MongoIngestionJobsRepository

    store = raw_store or build_raw_page_store(settings)
    jobs = MongoIngestionJobsRepository.from_settings(settings)

    def _run(request: CrawlRequest) -> CrawlResult:
        if (
            settings.source_type.lower() == "atlassian"
            and request.access_token
            and request.cloud_id
        ):
            adapter: DocumentSourceAdapter = build_request_source_adapter(
                settings, cloud_id=request.cloud_id, access_token=request.access_token
            )
        elif fallback_source is not None:
            adapter = fallback_source
        else:
            raise RuntimeError(
                "full crawl 어댑터를 결정할 수 없다 — 요청에 accessToken/cloudId 가 없고 "
                "Settings 자격증명(RAG_ATLASSIAN_CLOUD_ID/RAG_ATLASSIAN_ACCESS_TOKEN)도 비어 있다"
            )
        connection, channel = open_rabbitmq_channel(settings)
        try:
            return run_full_crawl(
                request,
                raw_store=store,
                publisher=PikaQueuePublisher(channel),
                adapter=adapter,
                jobs=jobs,
            )
        finally:
            connection.close()

    return _run


def build_real_delta_runner(
    settings: Settings,
    *,
    raw_store: RawPageStore | None = None,
) -> Callable[[DeltaSyncRequest], DeltaSyncResult]:
    """운영 delta 러너 — ``build_delta_runner`` 에 잡 단위 RabbitMQ 연결을 입힌다.

    종전 ``IngestDeps.run_delta`` 기본값(``_poc_empty_delta``)은 운영에서도 항상
    "변경 0건" 을 반환했다. 본 러너는 실 raw_store + 실 발행으로 변경분이 chunking
    worker 까지 흐르게 한다. 연결은 잡마다 열고 닫는다(BackgroundTasks threadpool 안전).
    """
    store = raw_store or build_raw_page_store(settings)

    def _run(request: DeltaSyncRequest) -> DeltaSyncResult:
        from app.ingestion.workers.publisher import PikaQueuePublisher

        connection, channel = open_rabbitmq_channel(settings)
        try:
            runner = build_delta_runner(
                settings,
                raw_store=store,
                queue_publisher=PikaQueuePublisher(channel),
            )
            return runner(request)
        finally:
            connection.close()

    return _run


class _PerPublishPikaPublisher(QueuePublisher):
    """발행 1회마다 연결을 열고 닫는 publisher — completion event(잡당 1건) 전용.

    BackgroundTasks threadpool 에서 여러 잡이 동시에 terminal 상태에 도달해도 연결을
    공유하지 않아 thread-safe 하다. 고빈도 발행 경로(content.chunking)에는 쓰지 않는다.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def publish(self, *, routing_key: str, message: dict[str, object]) -> None:
        from app.ingestion.workers.publisher import PikaQueuePublisher

        connection, channel = open_rabbitmq_channel(self._settings)
        try:
            PikaQueuePublisher(channel).publish(routing_key=routing_key, message=message)
        finally:
            connection.close()


def build_real_completion_publisher(settings: Settings) -> IngestCompletionPublisher:
    """운영 completion event publisher — 잡 terminal 상태를 실 RabbitMQ 로 발행한다.

    종전 ``IngestDeps.completion_publisher`` 기본값(Noop)은 운영에서도 이벤트를 발행하지
    않아 BFF 의 Admin Key 말소 트리거(api-spec v2.5.0)가 영원히 오지 않았다.
    """
    return build_ingest_completion_publisher(_PerPublishPikaPublisher(settings), settings)
