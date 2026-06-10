"""Chunking + Embedding Worker (FR-003/FR-004) — content.chunking 소비 [Pipeline].

--------------------------------------------------
작성자 : 최태성
작성목적 : Data Ingestion Agent(FR-001)가 발행한 ``content.chunking`` 메시지를 소비해
          ``raw_pages`` 본문을 로드 → Adaptive Chunker(chunk_page) → Dual Embedding +
          Multi-Pool Qdrant upsert(index_chunks, embedding_cache 멱등성)까지 한 흐름으로
          처리한다. 복사 자산(chunker/embedder/indexer)이 이미 embed+upsert 를 결합하므로
          PoC 는 단일 Worker 토폴로지로 배선한다(featureI-4 결정 — content.embedding 큐는
          상수로 예약). 단계 결과는 ``ingestion_jobs`` 에 기록한다(`docs/db-schema.md` §2.3).
작성일 : 2026-05-26 (featureI-4)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-26, featureI-4, 단일 Chunking+Embedding Worker 배선 — process_chunking_message
    (raw_pages 로드 → chunk_page → index_chunks → ingestion_jobs) + run_chunking_worker 루프.
  - 2026-05-26, featureI-3b, 첨부 청킹 경로 배선 — ``source_type`` 분기로 본문/첨부 처리를
    나눈다. 첨부는 raw_attachments 로드 → analyze_attachment(유형·유효성) → chunk_attachment
    (파일 직접 읽기) → index_chunks(attachment_download_urls) 흐름이다. rag 레포의 ingestion
    그래프와 동일하게 파일 기반 chunk_attachment 를 사용하며 별도 attachment_texts 컬렉션은
    청킹 경로에 두지 않는다(extracted_text 는 raw_attachments 에 함께 적재).
  - 2026-06-09, FR-002 첨부 다운로더 배선 — ``ChunkingWorkerDeps.attachment_downloader`` 추가.
    chunk_attachment 전에 ``ensure_local`` 로 download_url→local_path 를 채운다(기본 None=생략).
  - 2026-06-10, 코드 리뷰 재점검(A4·P1-2) — poison-message 격리 보강: (1) 다운로드 실패
    (``AttachmentDownloadError``)를 첨부 단위 ``ATTACH_DOWNLOAD_FAILED`` 로 격리(전파 시
    nack/DLQ 부재로 무한 재전송 루프). (2) 추출 라이브러리의 비-ValueError 예외(openpyxl
    InvalidFileException·python-docx PackageNotFoundError·PyMuPDF FileDataError 등)도 첨부
    단위 UNSUPPORTED_ATTACH_TYPE 로 격리. (3) malformed 메시지(평 KeyError)를 루프에서
    메시지 단위 격리(ERROR 로그 후 skip).
--------------------------------------------------
구현 메모(featureI-4/I-3b):
  - 외부 의존성(임베더/Qdrant/cache/raw_store/jobs)은 주입 가능하게 둔다(테스트는 Fake).
    실 어댑터(E5/BM25/Qdrant from_settings) 부트스트랩은 배포 wiring(후속).
  - doc_type 은 chunk_page 의 라벨 휴리스틱 폴백을 사용한다. GPT-4o-mini 문서 분석기[Agent]
    는 featureI-4b 후속.
  - 첨부: ``chunk_attachment`` 는 첨부 파일을 파일 시스템에서 직접 읽으므로(fitz/openpyxl/
    python-docx) ``deps.chunk_attachment_fn`` 으로 주입 가능하게 둔다(테스트는 파일 시스템
    의존성 회피용 fake 주입 — rag IngestionGraphDeps.chunk_attachment_fn 패턴 정합).
  - ACL 누락 페이지는 색인하지 않는다(INVALID_ACL — app/CLAUDE.md §3). 첨부는 부모 페이지
    ACL 을 상속한다. 토큰·자격증명은 메시지·로그에 남기지 않는다.
--------------------------------------------------
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.ingestion.attachment_analyzer import analyze_attachment
from app.ingestion.attachment_downloader import AttachmentDownloader, AttachmentDownloadError
from app.ingestion.chunker import chunk_attachment, chunk_page
from app.ingestion.indexer import index_chunks
from app.ingestion.workers.consumer import MessageConsumer
from app.schemas.chunk import Chunk
from app.schemas.enums import IngestionStage, IngestionStatus, SourceType
from app.storage.jobs import IngestionJobRecord, IngestionJobsRepository
from app.storage.raw_store import RawPageStore

_LOGGER = logging.getLogger(__name__)

# chunk_attachment 시그니처 — 파일 시스템 의존성을 갖는 함수라 deps 주입으로 테스트한다
# (rag app/pipeline/ingestion_graph.py 의 ChunkAttachmentFn 패턴 정합).
ChunkAttachmentFn = Callable[..., list[Chunk]]


class RawPageNotFoundError(KeyError):
    """``content.chunking`` 메시지의 ``page_id`` 가 ``raw_pages`` 에 없을 때(파이프라인 불일치)."""


class AttachmentNotFoundError(KeyError):
    """첨부 메시지의 ``attachment_id`` 가 ``raw_attachments`` 에 없을 때(파이프라인 불일치)."""


@dataclass(slots=True)
class ChunkingWorkerDeps:
    """Chunking Worker 가 사용하는 주입 의존성 묶음.

    Attributes:
        raw_store: ``raw_pages`` 조회 어댑터(``get_page``).
        dense_embedder / sparse_embedder: Dual Embedding 어댑터(테스트는 Fake).
        store: Qdrant Multi-Pool 저장소(``index_chunks`` 가 upsert).
        cache: ``embedding_cache`` 어댑터(멱등성).
        jobs: ``ingestion_jobs`` 기록 어댑터. None 이면 기록을 생략한다.
        chunk_lookup: ``chunk_lookup`` 어댑터. None 이면 적재를 생략한다(legacy 호환).
        doc_type_resolver: 문서 분석기[Agent](`DocumentAnalyzer`). 주입 시 스페이스 단위
            LLM doc_type 으로 청킹하고, None 이면 chunk_page 의 라벨 휴리스틱 폴백을 쓴다.
        chunk_attachment_fn: 첨부 청킹 함수. 기본값은 파일을 직접 읽는 ``chunk_attachment``
            이며, 테스트는 파일 시스템 의존성 회피용 fake 를 주입한다(featureI-3b).
    """

    raw_store: RawPageStore
    dense_embedder: Any
    sparse_embedder: Any
    store: Any
    cache: Any
    jobs: IngestionJobsRepository | None = None
    chunk_lookup: Any | None = None
    doc_type_resolver: Any | None = None
    chunk_attachment_fn: ChunkAttachmentFn = chunk_attachment
    # FR-002 — 첨부 다운로더. None(기본)이면 다운로드 단계 생략(fixture 는 local_path 보유).
    # 운영은 infra 진입점에서 HttpAttachmentDownloader 주입 → download_url 을 local_path 로 채운다.
    attachment_downloader: AttachmentDownloader | None = None


@dataclass(slots=True)
class ChunkingMessageResult:
    """``content.chunking`` 메시지 1건 처리 결과."""

    page_id: str
    status: IngestionStatus
    chunks: int = 0
    upserted: int = 0
    skipped: int = 0
    # 첨부 메시지일 때만 채워진다(본문 메시지는 None).
    attachment_id: str | None = None


def process_chunking_message(
    message: dict[str, Any], deps: ChunkingWorkerDeps
) -> ChunkingMessageResult:
    """``content.chunking`` 메시지 1건을 ``source_type`` 으로 분기해 처리한다(테스트 대상).

    ``source_type`` 이 ``attachment`` 면 첨부 청킹 경로, 그 외(기본 ``page``)는 본문 청킹
    경로로 위임한다. Full Crawl(crawler) 은 본문·첨부 메시지를 모두 ``content.chunking``
    으로 발행하며, 본 Worker 가 단일 큐에서 양쪽을 소비한다(featureI-3b).

    Args:
        message: 발행 메시지. 본문은 ``page_id`` 필수, 첨부는 ``page_id``+``attachment_id``
            필수(`build_chunking_message`/`build_attachment_chunking_message` 형식).
        deps: 주입 의존성 묶음.

    Returns:
        처리 결과(`ChunkingMessageResult`).

    Raises:
        RawPageNotFoundError: ``page_id`` 가 ``raw_pages`` 에 없을 때(상위에서 DLQ 처리).
        AttachmentNotFoundError: 첨부 메시지의 ``attachment_id`` 가 ``raw_attachments`` 에
            없을 때(상위에서 DLQ 처리).
    """
    source_type = str(message.get("source_type", SourceType.PAGE.value))
    if source_type == SourceType.ATTACHMENT.value:
        return _process_attachment_message(message, deps)
    return _process_page_message(message, deps)


def _process_page_message(
    message: dict[str, Any], deps: ChunkingWorkerDeps
) -> ChunkingMessageResult:
    """본문 청킹 경로 — raw_pages 로드 → ACL 게이트 → chunk_page → 빈 본문 게이트 → 색인.

    흐름: raw_pages 로드 → ACL 게이트(INVALID_ACL) → chunk_page(폴백 doc_type) →
    빈 본문 게이트(EMPTY_BODY) → index_chunks(embed+upsert+cache) → ingestion_jobs(SUCCESS).
    """
    page_id = str(message["page_id"])
    started_at = datetime.now(UTC)

    page = deps.raw_store.get_page(page_id)
    if page is None:
        raise RawPageNotFoundError(page_id)

    # ACL 게이트 — allowed_groups/users 가 모두 비면 색인하지 않는다(app/CLAUDE.md §3).
    if page.is_acl_missing:
        _record(deps, page_id, IngestionStatus.INVALID_ACL, started_at, error="ACL missing")
        return ChunkingMessageResult(page_id=page_id, status=IngestionStatus.INVALID_ACL)

    # doc_type: 분석기[Agent] 주입 시 스페이스 단위 LLM 판별, 미주입 시 라벨 휴리스틱 폴백.
    doc_type = (
        deps.doc_type_resolver.resolve_doc_type(page)
        if deps.doc_type_resolver is not None
        else None
    )
    chunks = chunk_page(page, doc_type)
    if not chunks:
        _record(deps, page_id, IngestionStatus.EMPTY_BODY, started_at, error="no chunks")
        return ChunkingMessageResult(page_id=page_id, status=IngestionStatus.EMPTY_BODY)

    result = index_chunks(
        chunks,
        version_by_page_id={page.page_id: page.version_number},
        dense_embedder=deps.dense_embedder,
        sparse_embedder=deps.sparse_embedder,
        store=deps.store,
        cache=deps.cache,
        chunk_lookup=deps.chunk_lookup,
    )
    _record(deps, page_id, IngestionStatus.SUCCESS, started_at, error=None)
    return ChunkingMessageResult(
        page_id=page_id,
        status=IngestionStatus.SUCCESS,
        chunks=len(chunks),
        upserted=result.upserted_count,
        skipped=result.skipped_count,
    )


def _process_attachment_message(
    message: dict[str, Any], deps: ChunkingWorkerDeps
) -> ChunkingMessageResult:
    """첨부 청킹 경로 — raw_attachments 로드 → ACL 게이트(부모 상속) → analyze → chunk → 색인.

    흐름: raw_pages/raw_attachments 로드 → 부모 페이지 ACL 게이트(INVALID_ACL) →
    analyze_attachment(유형·텍스트 유효성, 미통과 시 그 status 기록 후 스킵) →
    chunk_attachment(파일 직접 읽기, ValueError 는 ATTACH_ENCRYPTED/UNSUPPORTED 로 격리) →
    index_chunks(attachment_download_urls) → ingestion_jobs(SUCCESS).

    rag 레포 ingestion 그래프의 첨부 처리와 동일하게 파일 기반 ``chunk_attachment`` 를 쓴다.
    첨부 청크는 부모 페이지의 ACL·space_key·labels 를 상속한다(`build_attachment_metadata`).
    """
    page_id = str(message["page_id"])
    attachment_id = str(message["attachment_id"])
    started_at = datetime.now(UTC)

    page = deps.raw_store.get_page(page_id)
    if page is None:
        raise RawPageNotFoundError(page_id)
    attachment = deps.raw_store.get_attachment(attachment_id)
    if attachment is None:
        raise AttachmentNotFoundError(attachment_id)

    # ACL 게이트 — 첨부는 부모 페이지 ACL 을 상속한다(부모가 ACL 누락이면 색인 금지).
    if page.is_acl_missing:
        _record(
            deps,
            page_id,
            IngestionStatus.INVALID_ACL,
            started_at,
            error="ACL missing",
            attachment_id=attachment_id,
        )
        return ChunkingMessageResult(
            page_id=page_id, status=IngestionStatus.INVALID_ACL, attachment_id=attachment_id
        )

    # 첨부 분석 — 유형 판별 + 텍스트 유효성(결정론, LLM 미호출). 미통과 시 그 status 기록 후 스킵.
    analysis = analyze_attachment(attachment)
    if not analysis.analyzable:
        _record(
            deps,
            page_id,
            analysis.status,
            started_at,
            error=analysis.reason,
            stage=IngestionStage.ANALYZE,
            attachment_id=attachment_id,
        )
        return ChunkingMessageResult(
            page_id=page_id, status=analysis.status, attachment_id=attachment_id
        )

    # 첨부 파일이 로컬에 없으면(운영 어댑터는 download_url 만 제공) 다운로더로 받아 local_path 를
    # 채운다(FR-002). 다운로드 실패는 다운로더 내부 제한 재시도를 소진한 결과이므로 첨부 단위로
    # 격리한다(A4) — consumer 에 nack/DLQ 가 없어 전파 시 배치 중단 + 무한 재전송 poison 루프.
    if deps.attachment_downloader is not None:
        try:
            attachment = deps.attachment_downloader.ensure_local(attachment)
        except AttachmentDownloadError as exc:
            _record(
                deps,
                page_id,
                IngestionStatus.ATTACH_DOWNLOAD_FAILED,
                started_at,
                error=str(exc),
                stage=IngestionStage.CHUNK,
                attachment_id=attachment_id,
            )
            return ChunkingMessageResult(
                page_id=page_id,
                status=IngestionStatus.ATTACH_DOWNLOAD_FAILED,
                attachment_id=attachment_id,
            )

    # 첨부 청킹 — chunk_attachment 는 첨부 파일을 직접 읽는다(테스트는 chunk_attachment_fn 주입).
    try:
        chunks = deps.chunk_attachment_fn(attachment, page, analysis.attachment_type)
    except ValueError as exc:
        # 암호화 PDF(ATTACH_ENCRYPTED) / 미지원 유형은 첨부 단위로 격리(본문·다른 첨부 무영향).
        status = (
            IngestionStatus.ATTACH_ENCRYPTED
            if "ATTACH_ENCRYPTED" in str(exc)
            else IngestionStatus.UNSUPPORTED_ATTACH_TYPE
        )
        _record(
            deps,
            page_id,
            status,
            started_at,
            error=str(exc),
            stage=IngestionStage.CHUNK,
            attachment_id=attachment_id,
        )
        return ChunkingMessageResult(page_id=page_id, status=status, attachment_id=attachment_id)
    except Exception as exc:  # noqa: BLE001 — 추출 라이브러리 예외 격리(P1-2)
        # 손상 파일이 openpyxl InvalidFileException / python-docx PackageNotFoundError /
        # PyMuPDF FileDataError 등 라이브러리 고유 예외를 던진다 — ValueError 만 잡으면
        # 한 건이 페이지·배치 전체를 중단시키므로(poison) 첨부 단위로 격리한다.
        _LOGGER.warning(
            "chunking worker: 첨부 추출 실패로 첨부 단위 격리 — attachment_id=%s",
            attachment_id,
            exc_info=True,
        )
        _record(
            deps,
            page_id,
            IngestionStatus.UNSUPPORTED_ATTACH_TYPE,
            started_at,
            error=f"{type(exc).__name__}: {exc}",
            stage=IngestionStage.CHUNK,
            attachment_id=attachment_id,
        )
        return ChunkingMessageResult(
            page_id=page_id,
            status=IngestionStatus.UNSUPPORTED_ATTACH_TYPE,
            attachment_id=attachment_id,
        )

    if not chunks:
        # 추출 가능했으나 청크가 0건(빈 첨부) — 적재 회피, SUCCESS 로 종결한다.
        _record(
            deps,
            page_id,
            IngestionStatus.SUCCESS,
            started_at,
            error=None,
            attachment_id=attachment_id,
        )
        return ChunkingMessageResult(
            page_id=page_id, status=IngestionStatus.SUCCESS, attachment_id=attachment_id
        )

    result = index_chunks(
        chunks,
        version_by_page_id={page.page_id: page.version_number},
        dense_embedder=deps.dense_embedder,
        sparse_embedder=deps.sparse_embedder,
        store=deps.store,
        cache=deps.cache,
        chunk_lookup=deps.chunk_lookup,
        attachment_download_urls={attachment_id: attachment.download_url},
    )
    _record(
        deps, page_id, IngestionStatus.SUCCESS, started_at, error=None, attachment_id=attachment_id
    )
    return ChunkingMessageResult(
        page_id=page_id,
        status=IngestionStatus.SUCCESS,
        chunks=len(chunks),
        upserted=result.upserted_count,
        skipped=result.skipped_count,
        attachment_id=attachment_id,
    )


def run_chunking_worker(
    consumer: MessageConsumer, deps: ChunkingWorkerDeps
) -> list[ChunkingMessageResult]:
    """consumer 스트림의 ``content.chunking`` 메시지를 순서대로 처리한다(얇은 루프).

    각 메시지를 ``process_chunking_message`` 로 처리하고 성공 결과를 모아 반환한다.

    회복력: ``RawPageNotFoundError`` / ``AttachmentNotFoundError`` 는 메시지가 가리키는
    ``raw_pages``/``raw_attachments`` 가 없는 **파이프라인 불일치(영구 실패)** 로, 재시도해도
    해소되지 않는다. 이 예외가 루프 밖으로 전파되면 워커 배치 전체가 중단되고, consumer 가
    해당 메시지를 ack 하지 못해 재연결 시 무한 재전송되는 poison-message 가 된다. 따라서 이
    두 예외는 메시지 단위로 격리(WARNING 로그 후 skip)하고 다음 메시지를 계속 처리한다 —
    그래야 consumer 가 정상적으로 ack 를 이어가 poison 루프를 막는다. 이는
    ``process_chunking_message`` docstring 이 명시한 "상위에서 DLQ 처리" 의 최소 구현이다.

    범위: 영구 실패의 durable DLQ 적재와 전이적(transient) 실패의 재시도 분리는 후속
    (featureI DLQ 정책)이다. 예상치 못한 예외는 무음 데이터 유실을 피하기 위해 그대로
    전파한다(광역 ``except`` 로 삼키지 않는다). 단 **malformed 메시지(필수 키 누락 —
    평 ``KeyError``)는 재시도해도 해소되지 않는 영구 실패**라 메시지 단위로 격리한다
    (A4 — 전파 시 미ack 재전송 poison 루프).
    """
    results: list[ChunkingMessageResult] = []
    for message in consumer.consume():
        try:
            results.append(process_chunking_message(message, deps))
        except (RawPageNotFoundError, AttachmentNotFoundError) as exc:
            _LOGGER.warning(
                "chunking worker: 파이프라인 불일치로 메시지 1건 skip — %s: %s",
                type(exc).__name__,
                exc,
            )
        except KeyError as exc:
            # 필수 키(page_id/attachment_id) 누락 메시지 — 발행자 버그/스키마 불일치.
            _LOGGER.error(
                "chunking worker: malformed 메시지 1건 skip — 누락 키 %s (keys=%s)",
                exc,
                sorted(message.keys()),
            )
    return results


def _record(
    deps: ChunkingWorkerDeps,
    page_id: str,
    status: IngestionStatus,
    started_at: datetime,
    *,
    error: str | None,
    stage: IngestionStage = IngestionStage.UPSERT,
    attachment_id: str | None = None,
) -> None:
    """``ingestion_jobs`` 에 단계 결과를 기록한다(jobs 미주입 시 noop).

    단일 Worker 가 chunk→embed→upsert 를 결합 처리하므로 색인 시도는 종단 단계인
    ``upsert`` 로 1건 기록한다(stage enum 정합 — db-schema §2.3). 첨부 경로에서 분석·청킹
    단계가 먼저 실패하면 ``stage`` 를 ANALYZE/CHUNK 로 명시해 어디서 걸렸는지 남긴다.
    첨부 메시지는 ``attachment_id`` 를 채워 페이지 잡과 구분한다.
    """
    if deps.jobs is None:
        return
    deps.jobs.record(
        IngestionJobRecord(
            page_id=page_id,
            attachment_id=attachment_id,
            stage=stage,
            status=status,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            error=error,
        )
    )
