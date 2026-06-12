"""문서 분석기 [Agent] 단위 테스트 — doc_type 판별·캐싱·폴백 + Worker 연동.

작성자 : 최태성
담당 영역 : ingestion

LLM(OpenAI)·MySQL 은 Fake 로 대체한다(FakeDocTypeClassifier / FakeSpaceDocTypeCache).
스페이스 단위 1회 판별(캐시 히트 시 LLM 재호출 없음)·저신뢰/예외 폴백·Worker 가 resolver
doc_type 으로 청킹하는지를 검증한다.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from app.ingestion.document_analyzer import (
    FALLBACK_DOC_TYPE,
    DocTypeClassification,
    DocumentAnalyzer,
    FakeDocTypeClassifier,
)
from app.ingestion.embedder.base import (
    FakeDenseEmbedder,
    FakeSparseEmbedder,
    SparseVector,
)
from app.ingestion.vector_store import CONTENT_POOL
from app.ingestion.workers.chunking_worker import (
    ChunkingWorkerDeps,
    process_chunking_message,
)
from app.schemas.chunk import Chunk
from app.schemas.enums import DocType, IngestionStatus
from app.schemas.page_object import PageObject
from app.storage.mongo_cache import FakeEmbeddingCache
from app.storage.raw_store import FakeRawPageStore
from app.storage.space_doc_type_cache import FakeSpaceDocTypeCache


def _page(page_id: str = "page-1", *, space_key: str = "ENG") -> PageObject:
    return PageObject(
        page_id=page_id,
        space_key=space_key,
        title="Incident report",
        body_html="<h2>Outage</h2><p>The service went down at 02:00 and was restored.</p>",
        version_number=1,
        last_modified=datetime.fromisoformat("2026-05-14T01:00:00+00:00"),
        allowed_groups=["space:ENG"],
        allowed_users=[],
        webui_link="/wiki/page-1",
        labels=["operation"],  # 라벨 폴백이면 operation 이 되지만, resolver 가 우선한다.
    )


def _analyzer(classifier: FakeDocTypeClassifier, cache: FakeSpaceDocTypeCache) -> DocumentAnalyzer:
    return DocumentAnalyzer(classifier=classifier, cache=cache)


def test_cache_miss_classifies_and_caches() -> None:
    classifier = FakeDocTypeClassifier(
        result=DocTypeClassification(dominant=DocType.INCIDENT, confidence=0.92)
    )
    cache = FakeSpaceDocTypeCache()
    analyzer = _analyzer(classifier, cache)

    resolved = analyzer.resolve_doc_type(_page())

    assert resolved is DocType.INCIDENT
    assert classifier.calls == 1
    entry = cache.get("ENG")
    assert entry is not None
    assert entry.dominant_doc_type is DocType.INCIDENT
    assert entry.sample_count == 1


def test_cache_hit_reuses_without_reclassifying() -> None:
    classifier = FakeDocTypeClassifier(
        result=DocTypeClassification(dominant=DocType.FAQ, confidence=0.9)
    )
    cache = FakeSpaceDocTypeCache()
    analyzer = _analyzer(classifier, cache)

    first = analyzer.resolve_doc_type(_page("page-1", space_key="ENG"))
    second = analyzer.resolve_doc_type(_page("page-2", space_key="ENG"))

    assert first is DocType.FAQ
    assert second is DocType.FAQ
    # 스페이스 1회 판별 — 두 번째 페이지는 캐시 히트라 LLM 재호출이 없다.
    assert classifier.calls == 1


def test_low_confidence_falls_back_to_operation_and_caches() -> None:
    classifier = FakeDocTypeClassifier(
        result=DocTypeClassification(dominant=DocType.ADR, confidence=0.3)
    )
    cache = FakeSpaceDocTypeCache()
    analyzer = _analyzer(classifier, cache)

    resolved = analyzer.resolve_doc_type(_page())

    assert resolved is FALLBACK_DOC_TYPE  # operation
    entry = cache.get("ENG")
    assert entry is not None
    assert entry.dominant_doc_type is FALLBACK_DOC_TYPE
    assert entry.confidence == 0.3


def test_classifier_failure_falls_back_without_caching() -> None:
    classifier = FakeDocTypeClassifier(error=RuntimeError("llm timeout"))
    cache = FakeSpaceDocTypeCache()
    analyzer = _analyzer(classifier, cache)

    resolved = analyzer.resolve_doc_type(_page())

    assert resolved is FALLBACK_DOC_TYPE
    # 일시적 실패는 캐싱하지 않아 다음 페이지에서 재시도된다.
    assert cache.get("ENG") is None


class _DocTypeCapturingStore:
    """upsert 된 청크의 doc_type 을 캡처하는 fake Qdrant store(CONTENT_POOL 만 본다)."""

    def __init__(self) -> None:
        self.doc_types: list[str] = []

    def upsert_chunks_batch(
        self,
        pool_name: str,
        items: Iterable[tuple[Chunk, int, list[float], SparseVector]],
    ) -> None:
        if pool_name != CONTENT_POOL:
            return
        for chunk, _version, _dense, _sparse in items:
            self.doc_types.append(chunk.metadata.doc_type.value)


def test_worker_uses_resolver_doc_type_for_chunking() -> None:
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))  # 라벨은 operation
    store = _DocTypeCapturingStore()
    # resolver 가 incident(고신뢰)로 판별 → 라벨 폴백(operation)이 아니라 incident 로 청킹돼야 한다.
    resolver = _analyzer(
        FakeDocTypeClassifier(
            result=DocTypeClassification(dominant=DocType.INCIDENT, confidence=0.95)
        ),
        FakeSpaceDocTypeCache(),
    )
    deps = ChunkingWorkerDeps(
        raw_store=raw,
        dense_embedder=FakeDenseEmbedder(),
        sparse_embedder=FakeSparseEmbedder(),
        store=store,
        cache=FakeEmbeddingCache(),
        doc_type_resolver=resolver,
    )

    result = process_chunking_message({"page_id": "page-1"}, deps)

    assert result.status is IngestionStatus.SUCCESS
    assert result.chunks >= 1
    assert store.doc_types  # 최소 1개 청크 upsert
    assert all(doc_type == DocType.INCIDENT.value for doc_type in store.doc_types)
