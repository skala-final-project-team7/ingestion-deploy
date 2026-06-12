"""Ingestion 파이프라인 조립 (in-process PoC) [Pipeline].

--------------------------------------------------
작성자 : 최태성
담당 영역 : ingestion
작성목적 : 큐로 분리된 두 단계(Full Crawl → ``content.chunking`` 발행 // chunking_worker →
          Qdrant upsert)를 PoC·로컬·통합 테스트용으로 **in-process 합성**한다. 운영에서는
          RabbitMQ 로 분리된 독립 Worker 로 동작하지만(EKS 독립 스케일링), 본 모듈은 전체
          흐름을 한 번에 구동해 end-to-end 동작을 검증·시연한다.
작성일 : 2026-05-26 (featureI-7)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-26, 최초 작성, featureI-7 — run_ingestion_pipeline + build_poc_components +
    run_poc_ingestion(all-fakes 조립).
  - 2026-05-26, ADR 0003 항목 3 운영 wiring — crawl 단계가 worker 와 동일한 jobs 인스턴스를
    공유하도록 연결(CRAWL + UPSERT 가 한 ingestion_jobs 에 기록). PoC 조립에 jobs 추가.
  - 2026-05-26, featureI-3b — build_poc_components/run_poc_ingestion 에 chunk_attachment_fn
    주입 파라미터 추가. 첨부를 포함한 crawl→첨부 적재→첨부 청킹→Qdrant 전 체인을 파일 시스템
    의존성 없이 in-process 로 검증할 수 있다(기본값은 실 chunk_attachment).
--------------------------------------------------
[주의] 본 합성은 PoC/테스트용이다. 운영은 crawl 과 chunking_worker 를 큐로 분리해 별도
       스케일링한다(featureI-7b 배포 wiring). run_ingestion_pipeline 은 발행 메시지를
       in-process 로 drain 하므로 ``FakeQueuePublisher`` 를 전제로 한다.
--------------------------------------------------
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.adapters.base import DocumentSourceAdapter
from app.ingestion.crawler import CrawlRequest, CrawlResult, run_full_crawl
from app.ingestion.embedder.base import FakeDenseEmbedder, FakeSparseEmbedder
from app.ingestion.workers import QUEUE_CHUNKING
from app.ingestion.workers.chunking_worker import (
    ChunkAttachmentFn,
    ChunkingMessageResult,
    ChunkingWorkerDeps,
    run_chunking_worker,
)
from app.ingestion.workers.consumer import FakeMessageConsumer
from app.ingestion.workers.publisher import FakeQueuePublisher
from app.storage.jobs import FakeIngestionJobsRepository
from app.storage.mongo_cache import FakeEmbeddingCache
from app.storage.qdrant_fake import FakeQdrantPoolStore
from app.storage.raw_store import FakeRawPageStore


@dataclass
class PipelineResult:
    """in-process 파이프라인 실행 결과."""

    crawl: CrawlResult
    indexed: list[ChunkingMessageResult]


@dataclass
class PocComponents:
    """all-fakes PoC 구성 요소(테스트가 적재·발행 상태를 직접 검증)."""

    raw_store: FakeRawPageStore
    publisher: FakeQueuePublisher
    chunking_deps: ChunkingWorkerDeps
    store: FakeQdrantPoolStore
    jobs: FakeIngestionJobsRepository


def run_ingestion_pipeline(
    request: CrawlRequest,
    *,
    source: DocumentSourceAdapter,
    raw_store: Any,
    publisher: FakeQueuePublisher,
    chunking_deps: ChunkingWorkerDeps,
) -> PipelineResult:
    """Full Crawl → 발행 메시지 drain → chunking_worker 를 in-process 로 합성 실행한다.

    Args:
        request: Full Crawl 트리거 입력.
        source: 공급원 어댑터(crawl 입력). ``raw_store`` 는 crawl 적재와 worker 조회가
            **같은 인스턴스**여야 한다(메시지에는 page_id 만 싣고 본문은 raw_store 에서 로드).
        raw_store: crawl 적재 + worker 조회 공용 store(``chunking_deps.raw_store`` 와 동일 객체).
        publisher: ``FakeQueuePublisher`` — 발행된 ``content.chunking`` 메시지를 in-process drain.
        chunking_deps: chunking_worker 의존성(store/embedder/cache/jobs/…).

    Returns:
        crawl 집계 + 메시지별 색인 결과(`PipelineResult`).

    Note:
        crawl 단계 CRAWL 잡 기록은 worker 와 **동일한 jobs 인스턴스**(``chunking_deps.jobs``)를
        공유한다 — 한 ``ingestion_jobs`` 에 CRAWL(페이지별) + UPSERT 가 함께 남는다(ADR 0003
        항목 3). ``chunking_deps.jobs`` 가 None 이면 crawl 도 기록을 생략한다(비파괴).
    """
    crawl = run_full_crawl(
        request,
        raw_store=raw_store,
        publisher=publisher,
        adapter=source,
        jobs=chunking_deps.jobs,
    )
    chunking_messages = [
        message.body for message in publisher.messages if message.routing_key == QUEUE_CHUNKING
    ]
    consumer = FakeMessageConsumer(messages=chunking_messages)
    indexed = run_chunking_worker(consumer, chunking_deps)
    return PipelineResult(crawl=crawl, indexed=indexed)


def build_poc_components(
    *,
    doc_type_resolver: Any | None = None,
    chunk_attachment_fn: ChunkAttachmentFn | None = None,
) -> PocComponents:
    """all-fakes PoC 구성 요소를 만든다(raw_store 는 crawl·worker 공용으로 공유).

    ``chunk_attachment_fn`` 을 주입하면 첨부 청킹을 파일 시스템 없이 fake 로 구동할 수 있다
    (미주입 시 실 ``chunk_attachment``). 첨부 없는 본문-only PoC 는 주입하지 않아도 된다.
    """
    raw_store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    store = FakeQdrantPoolStore()
    # crawl(CRAWL)·worker(UPSERT)가 공유하는 단일 jobs 인스턴스 (ADR 0003 항목 3).
    jobs = FakeIngestionJobsRepository()
    deps_kwargs: dict[str, Any] = {
        "raw_store": raw_store,
        "dense_embedder": FakeDenseEmbedder(),
        "sparse_embedder": FakeSparseEmbedder(),
        "store": store,
        "cache": FakeEmbeddingCache(),
        "jobs": jobs,
        "doc_type_resolver": doc_type_resolver,
    }
    if chunk_attachment_fn is not None:
        deps_kwargs["chunk_attachment_fn"] = chunk_attachment_fn
    chunking_deps = ChunkingWorkerDeps(**deps_kwargs)
    return PocComponents(
        raw_store=raw_store,
        publisher=publisher,
        chunking_deps=chunking_deps,
        store=store,
        jobs=jobs,
    )


def run_poc_ingestion(
    request: CrawlRequest,
    source: DocumentSourceAdapter,
    *,
    doc_type_resolver: Any | None = None,
    chunk_attachment_fn: ChunkAttachmentFn | None = None,
) -> tuple[PipelineResult, PocComponents]:
    """all-fakes 조립으로 crawl→index 전 체인을 한 번에 구동한다(PoC/통합 테스트 편의)."""
    components = build_poc_components(
        doc_type_resolver=doc_type_resolver, chunk_attachment_fn=chunk_attachment_fn
    )
    result = run_ingestion_pipeline(
        request,
        source=source,
        raw_store=components.raw_store,
        publisher=components.publisher,
        chunking_deps=components.chunking_deps,
    )
    return result, components
