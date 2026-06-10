"""Chunking+Embedding Worker 단위 테스트 — content.chunking → Qdrant upsert end-to-end.

실제 Adaptive Chunker(chunk_page) + indexer.index_chunks 를 구동하고, 외부 모델·Qdrant·
Mongo 는 Fake 로 대체한다(FakeDenseEmbedder/FakeSparseEmbedder/FakeEmbeddingCache +
in-memory fake Qdrant store). 멱등성·ACL/빈 본문 게이트·잡 기록을 검증한다.
코드 리뷰 재점검(A4·P1-2) 후속으로 poison-message 격리도 검증한다 — 다운로드 실패
(ATTACH_DOWNLOAD_FAILED)·비-ValueError 추출 예외(UNSUPPORTED_ATTACH_TYPE)는 첨부 단위,
malformed 메시지(필수 키 누락)는 메시지 단위로 격리되고 배치는 계속된다.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from app.ingestion.attachment_downloader import AttachmentDownloadError
from app.ingestion.chunker import ChunkDraft, build_attachment_metadata
from app.ingestion.embedder.base import (
    FakeDenseEmbedder,
    FakeSparseEmbedder,
    SparseVector,
)
from app.ingestion.vector_store import POOL_NAMES
from app.ingestion.workers.chunking_worker import (
    AttachmentNotFoundError,
    ChunkingWorkerDeps,
    RawPageNotFoundError,
    process_chunking_message,
    run_chunking_worker,
)
from app.ingestion.workers.consumer import FakeMessageConsumer
from app.schemas.chunk import Chunk
from app.schemas.enums import (
    AttachmentType,
    ExtractedFormat,
    IngestionStage,
    IngestionStatus,
    SourceType,
)
from app.schemas.page_object import Attachment, PageObject
from app.storage.jobs import FakeIngestionJobsRepository
from app.storage.mongo_cache import FakeEmbeddingCache
from app.storage.raw_store import FakeRawPageStore


class _FakeQdrantStore:
    """index_chunks 가 호출하는 upsert_chunks_batch 만 캡처하는 in-memory fake."""

    def __init__(self) -> None:
        self.upserts: dict[str, list[str]] = {pool: [] for pool in POOL_NAMES}

    def upsert_chunks_batch(
        self,
        pool_name: str,
        items: Iterable[tuple[Chunk, int, list[float], SparseVector]],
    ) -> None:
        for chunk, _version, _dense, _sparse in items:
            self.upserts[pool_name].append(chunk.metadata.chunk_id)


def _page(page_id: str = "page-1", *, acl: bool = True, body: str | None = None) -> PageObject:
    body_html = (
        "<h2>Restart Procedure</h2>"
        "<p>Stop the service, clear the cache, then start it again and verify health.</p>"
        if body is None
        else body
    )
    return PageObject(
        page_id=page_id,
        space_key="ENG",
        title="Runbook",
        body_html=body_html,
        version_number=3,
        last_modified=datetime.fromisoformat("2026-05-14T01:00:00+00:00"),
        allowed_groups=["space:ENG"] if acl else [],
        allowed_users=[],
        webui_link=f"/wiki/{page_id}",
        labels=["operation"],
    )


def _deps(
    store: FakeRawPageStore,
    *,
    jobs: FakeIngestionJobsRepository | None = None,
    cache: FakeEmbeddingCache | None = None,
    qdrant: _FakeQdrantStore | None = None,
):
    return ChunkingWorkerDeps(
        raw_store=store,
        dense_embedder=FakeDenseEmbedder(),
        sparse_embedder=FakeSparseEmbedder(),
        store=qdrant or _FakeQdrantStore(),
        cache=cache or FakeEmbeddingCache(),
        jobs=jobs,
    )


def test_process_message_chunks_embeds_and_upserts() -> None:
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    qdrant = _FakeQdrantStore()
    jobs = FakeIngestionJobsRepository()
    deps = _deps(raw, jobs=jobs, qdrant=qdrant)

    result = process_chunking_message({"page_id": "page-1"}, deps)

    assert result.status is IngestionStatus.SUCCESS
    assert result.chunks >= 1
    assert result.upserted == result.chunks
    # 3개 Pool 모두에 동일 청크 수가 upsert 된다.
    for pool in POOL_NAMES:
        assert len(qdrant.upserts[pool]) == result.chunks
    # ingestion_jobs 에 upsert 단계 SUCCESS 1건 기록.
    assert len(jobs.records) == 1
    assert jobs.records[0].stage is IngestionStage.UPSERT
    assert jobs.records[0].status is IngestionStatus.SUCCESS
    assert jobs.records[0].page_id == "page-1"


def test_reindex_same_version_is_idempotent_skip() -> None:
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    cache = FakeEmbeddingCache()
    deps_first = _deps(raw, cache=cache)
    first = process_chunking_message({"page_id": "page-1"}, deps_first)
    assert first.upserted >= 1

    # 같은 cache 재사용 → 동일 (chunk_id, version) 캐시 히트로 스킵.
    deps_second = _deps(raw, cache=cache)
    second = process_chunking_message({"page_id": "page-1"}, deps_second)
    assert second.status is IngestionStatus.SUCCESS
    assert second.upserted == 0
    assert second.skipped == first.upserted


def test_acl_missing_page_is_not_indexed() -> None:
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1", acl=False))
    qdrant = _FakeQdrantStore()
    jobs = FakeIngestionJobsRepository()
    deps = _deps(raw, jobs=jobs, qdrant=qdrant)

    result = process_chunking_message({"page_id": "page-1"}, deps)

    assert result.status is IngestionStatus.INVALID_ACL
    assert result.upserted == 0
    assert all(qdrant.upserts[pool] == [] for pool in POOL_NAMES)
    assert jobs.records[0].status is IngestionStatus.INVALID_ACL


def test_empty_body_page_yields_empty_body_status() -> None:
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1", body=""))
    deps = _deps(raw)

    result = process_chunking_message({"page_id": "page-1"}, deps)

    assert result.status is IngestionStatus.EMPTY_BODY
    assert result.chunks == 0


def test_missing_raw_page_raises() -> None:
    deps = _deps(FakeRawPageStore())
    try:
        process_chunking_message({"page_id": "ghost"}, deps)
    except RawPageNotFoundError as exc:
        assert "ghost" in str(exc)
    else:  # pragma: no cover - 실패 시에만
        raise AssertionError("RawPageNotFoundError expected")


def test_run_worker_processes_all_consumer_messages() -> None:
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_page(_page("page-2"))
    consumer = FakeMessageConsumer(messages=[{"page_id": "page-1"}, {"page_id": "page-2"}])
    deps = _deps(raw)

    results = run_chunking_worker(consumer, deps)

    assert [r.page_id for r in results] == ["page-1", "page-2"]
    assert all(r.status is IngestionStatus.SUCCESS for r in results)


def test_run_worker_isolates_missing_page_and_continues() -> None:
    # 파이프라인 불일치 메시지(ghost)가 중간에 끼어 있어도 워커가 크래시하지 않고
    # 정상 메시지를 계속 처리해야 한다(poison-message 격리). process_chunking_message 는
    # 여전히 RawPageNotFoundError 를 raise 하지만, run_chunking_worker 가 메시지 단위로
    # 격리(로그 후 skip)한다.
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_page(_page("page-2"))
    consumer = FakeMessageConsumer(
        messages=[{"page_id": "page-1"}, {"page_id": "ghost"}, {"page_id": "page-2"}]
    )
    deps = _deps(raw)

    # 예외가 전파되지 않아야 한다(배치 크래시 없음).
    results = run_chunking_worker(consumer, deps)

    # ghost 는 격리(skip)되고 정상 2건만 결과에 남는다.
    assert [r.page_id for r in results] == ["page-1", "page-2"]
    assert all(r.status is IngestionStatus.SUCCESS for r in results)


def test_run_worker_isolates_missing_attachment_and_continues() -> None:
    # 첨부 불일치(AttachmentNotFoundError)도 동일하게 격리되어야 한다.
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_page(_page("page-2"))
    consumer = FakeMessageConsumer(
        messages=[
            {"page_id": "page-1"},
            {"page_id": "page-1", "attachment_id": "ghost-att", "source_type": "attachment"},
            {"page_id": "page-2"},
        ]
    )
    deps = _deps(raw)

    results = run_chunking_worker(consumer, deps)

    assert [r.page_id for r in results] == ["page-1", "page-2"]
    assert all(r.status is IngestionStatus.SUCCESS for r in results)


def test_run_worker_skips_malformed_message_and_continues() -> None:
    """A4 — 필수 키 누락(평 KeyError) 메시지는 영구 실패라 메시지 단위로 격리(skip)된다.

    종전에는 KeyError 가 전파돼 배치가 중단되고 미ack 재전송 poison 루프가 됐다.
    """
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_page(_page("page-2"))
    consumer = FakeMessageConsumer(
        messages=[
            {"page_id": "page-1"},
            {"space_key": "ENG"},  # page_id 누락 — 발행자 버그/스키마 불일치
            {"source_type": "attachment", "page_id": "page-1"},  # attachment_id 누락
            {"page_id": "page-2"},
        ]
    )
    deps = _deps(raw)

    results = run_chunking_worker(consumer, deps)  # 예외가 전파되지 않아야 한다.

    # malformed 2건은 skip 되고 정상 2건만 결과에 남는다.
    assert [r.page_id for r in results] == ["page-1", "page-2"]
    assert all(r.status is IngestionStatus.SUCCESS for r in results)


# --- 첨부 청킹 경로 (featureI-3b) ---

# analyze_attachment 의 텍스트 유효성(>= 200자) 통과를 위한 충분히 긴 정상 텍스트.
_LONG_TEXT = (
    "This runbook describes the database failover procedure in detail. Follow each "
    "numbered step carefully and verify the health checks between steps. Escalate to the "
    "on-call engineer if replication lag exceeds the configured threshold during failover."
)


def _attachment(
    attachment_id: str = "att-1",
    *,
    page_id: str = "page-1",
    filename: str = "runbook.pdf",
    mime: str = "application/pdf",
    text: str = _LONG_TEXT,
    download_url: str | None = None,
) -> Attachment:
    return Attachment(
        attachment_id=attachment_id,
        filename=filename,
        mime_type=mime,
        extracted_text=text,
        extracted_format=ExtractedFormat.RAW_TEXT,
        download_url=(
            download_url
            if download_url is not None
            else f"https://confluence.example/download/{attachment_id}"
        ),
        parent_page_id=page_id,
        last_modified=datetime.fromisoformat("2026-05-14T01:00:00+00:00"),
    )


def _fake_chunk_attachment(
    attachment: Attachment, page: PageObject, attachment_type: AttachmentType
) -> list[Chunk]:
    """파일 시스템 없이 결정론 첨부 청크 1건을 만든다(실 chunk_attachment 대체 주입)."""
    draft = ChunkDraft(text="Failover step body text.", section_header="Failover", is_atomic=False)
    meta = build_attachment_metadata(page, attachment, draft, 0, attachment_type)
    return [Chunk(text=draft.text, metadata=meta)]


def _att_deps(
    raw: FakeRawPageStore,
    *,
    chunk_fn=_fake_chunk_attachment,
    jobs: FakeIngestionJobsRepository | None = None,
    cache: FakeEmbeddingCache | None = None,
    qdrant: _FakeQdrantStore | None = None,
    downloader=None,
) -> ChunkingWorkerDeps:
    return ChunkingWorkerDeps(
        raw_store=raw,
        dense_embedder=FakeDenseEmbedder(),
        sparse_embedder=FakeSparseEmbedder(),
        store=qdrant or _FakeQdrantStore(),
        cache=cache or FakeEmbeddingCache(),
        jobs=jobs,
        chunk_attachment_fn=chunk_fn,
        attachment_downloader=downloader,
    )


_ATT_MSG = {"page_id": "page-1", "attachment_id": "att-1", "source_type": "attachment"}


def test_attachment_message_chunks_and_upserts() -> None:
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_attachment(_attachment("att-1", page_id="page-1"))
    qdrant = _FakeQdrantStore()
    jobs = FakeIngestionJobsRepository()
    deps = _att_deps(raw, jobs=jobs, qdrant=qdrant)

    result = process_chunking_message(_ATT_MSG, deps)

    assert result.status is IngestionStatus.SUCCESS
    assert result.attachment_id == "att-1"
    assert result.chunks == 1
    assert result.upserted == 1
    for pool in POOL_NAMES:
        assert len(qdrant.upserts[pool]) == 1
    # ingestion_jobs 에 첨부 UPSERT SUCCESS 1건 (attachment_id 채워짐).
    assert len(jobs.records) == 1
    assert jobs.records[0].stage is IngestionStage.UPSERT
    assert jobs.records[0].status is IngestionStatus.SUCCESS
    assert jobs.records[0].page_id == "page-1"
    assert jobs.records[0].attachment_id == "att-1"


def test_attachment_inherits_parent_acl_and_is_blocked_when_missing() -> None:
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1", acl=False))
    raw.save_attachment(_attachment("att-1", page_id="page-1"))
    qdrant = _FakeQdrantStore()
    jobs = FakeIngestionJobsRepository()
    deps = _att_deps(raw, jobs=jobs, qdrant=qdrant)

    result = process_chunking_message(_ATT_MSG, deps)

    assert result.status is IngestionStatus.INVALID_ACL
    assert result.upserted == 0
    assert all(qdrant.upserts[pool] == [] for pool in POOL_NAMES)
    assert jobs.records[0].status is IngestionStatus.INVALID_ACL
    assert jobs.records[0].attachment_id == "att-1"


def test_attachment_unsupported_type_recorded_and_skipped() -> None:
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_attachment(
        _attachment("att-zip", page_id="page-1", filename="data.zip", mime="application/zip")
    )
    qdrant = _FakeQdrantStore()
    jobs = FakeIngestionJobsRepository()
    deps = _att_deps(raw, jobs=jobs, qdrant=qdrant)

    result = process_chunking_message(
        {"page_id": "page-1", "attachment_id": "att-zip", "source_type": "attachment"}, deps
    )

    assert result.status is IngestionStatus.UNSUPPORTED_ATTACH_TYPE
    assert result.upserted == 0
    assert all(qdrant.upserts[pool] == [] for pool in POOL_NAMES)
    # 분석 단계에서 걸렸으므로 stage 는 ANALYZE.
    assert jobs.records[0].stage is IngestionStage.ANALYZE
    assert jobs.records[0].status is IngestionStatus.UNSUPPORTED_ATTACH_TYPE


def test_attachment_low_quality_text_recorded_and_skipped() -> None:
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_attachment(_attachment("att-low", page_id="page-1", text="too short"))
    jobs = FakeIngestionJobsRepository()
    deps = _att_deps(raw, jobs=jobs)

    result = process_chunking_message(
        {"page_id": "page-1", "attachment_id": "att-low", "source_type": "attachment"}, deps
    )

    assert result.status is IngestionStatus.LOW_QUALITY_ATTACH
    assert result.upserted == 0
    assert jobs.records[0].stage is IngestionStage.ANALYZE
    assert jobs.records[0].status is IngestionStatus.LOW_QUALITY_ATTACH


def test_attachment_encrypted_pdf_value_error_isolated() -> None:
    def _raise_encrypted(*_args: object) -> list[Chunk]:
        raise ValueError("ATTACH_ENCRYPTED: 암호화된 PDF는 처리할 수 없습니다")

    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_attachment(_attachment("att-1", page_id="page-1"))
    jobs = FakeIngestionJobsRepository()
    deps = _att_deps(raw, chunk_fn=_raise_encrypted, jobs=jobs)

    result = process_chunking_message(_ATT_MSG, deps)

    assert result.status is IngestionStatus.ATTACH_ENCRYPTED
    assert result.upserted == 0
    assert jobs.records[0].stage is IngestionStage.CHUNK
    assert jobs.records[0].status is IngestionStatus.ATTACH_ENCRYPTED


def test_attachment_other_value_error_maps_to_unsupported() -> None:
    def _raise_other(*_args: object) -> list[Chunk]:
        raise ValueError("지원하지 않는 첨부 유형")

    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_attachment(_attachment("att-1", page_id="page-1"))
    deps = _att_deps(raw, chunk_fn=_raise_other)

    result = process_chunking_message(_ATT_MSG, deps)

    assert result.status is IngestionStatus.UNSUPPORTED_ATTACH_TYPE
    assert result.upserted == 0


def test_attachment_non_value_error_extraction_isolated_as_unsupported() -> None:
    """P1-2 — 추출 라이브러리 고유 예외(비-ValueError)도 전파 대신 첨부 단위로 격리된다."""

    class _CorruptFileError(RuntimeError):
        """openpyxl InvalidFileException 류의 라이브러리 고유 예외 모사."""

    def _raise_library_error(*_args: object) -> list[Chunk]:
        raise _CorruptFileError("File is not a zip file")

    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_attachment(_attachment("att-1", page_id="page-1"))
    jobs = FakeIngestionJobsRepository()
    deps = _att_deps(raw, chunk_fn=_raise_library_error, jobs=jobs)

    result = process_chunking_message(_ATT_MSG, deps)  # 예외가 전파되지 않아야 한다.

    assert result.status is IngestionStatus.UNSUPPORTED_ATTACH_TYPE
    assert result.upserted == 0
    assert jobs.records[0].stage is IngestionStage.CHUNK
    assert jobs.records[0].status is IngestionStatus.UNSUPPORTED_ATTACH_TYPE
    # 예외 유형·메시지가 ingestion_jobs.error 에 남아 원인 추적이 가능하다.
    assert "_CorruptFileError" in (jobs.records[0].error or "")


# --- 첨부 다운로드 실패 격리 (FR-002 + 코드 리뷰 A4) ---


class _FailingDownloader:
    """항상 AttachmentDownloadError 를 던지는 다운로더 — 재시도 소진 상황 모사."""

    def ensure_local(self, attachment: Attachment) -> Attachment:
        raise AttachmentDownloadError(f"download failed: id={attachment.attachment_id}")


def test_attachment_download_failure_isolated_as_attach_download_failed() -> None:
    """A4 — 다운로드 실패는 전파 대신 ATTACH_DOWNLOAD_FAILED(stage=CHUNK)로 격리된다."""
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_attachment(_attachment("att-1", page_id="page-1"))
    qdrant = _FakeQdrantStore()
    jobs = FakeIngestionJobsRepository()
    deps = _att_deps(raw, jobs=jobs, qdrant=qdrant, downloader=_FailingDownloader())

    result = process_chunking_message(_ATT_MSG, deps)  # 예외가 전파되지 않아야 한다.

    assert result.status is IngestionStatus.ATTACH_DOWNLOAD_FAILED
    assert result.attachment_id == "att-1"
    assert result.upserted == 0
    assert all(qdrant.upserts[pool] == [] for pool in POOL_NAMES)
    assert jobs.records[0].stage is IngestionStage.CHUNK
    assert jobs.records[0].status is IngestionStatus.ATTACH_DOWNLOAD_FAILED
    assert jobs.records[0].attachment_id == "att-1"


def test_run_worker_continues_after_attachment_download_failure() -> None:
    """A4 — 다운로드 실패 첨부 1건이 배치를 중단시키지 않고 다음 메시지가 계속 처리된다."""

    class _SelectiveDownloader:
        """att-bad 만 실패시키고 나머지는 통과시키는 다운로더."""

        def ensure_local(self, attachment: Attachment) -> Attachment:
            if attachment.attachment_id == "att-bad":
                raise AttachmentDownloadError("download failed: id=att-bad")
            return attachment

    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_attachment(_attachment("att-bad", page_id="page-1"))
    raw.save_attachment(_attachment("att-ok", page_id="page-1"))
    consumer = FakeMessageConsumer(
        messages=[
            {"page_id": "page-1", "attachment_id": "att-bad", "source_type": "attachment"},
            {"page_id": "page-1", "attachment_id": "att-ok", "source_type": "attachment"},
            {"page_id": "page-1"},
        ]
    )
    deps = _att_deps(raw, downloader=_SelectiveDownloader())

    results = run_chunking_worker(consumer, deps)

    # 실패 건도 결과로 반환되고(격리 status), 이후 첨부·본문 메시지가 모두 처리된다.
    assert [r.status for r in results] == [
        IngestionStatus.ATTACH_DOWNLOAD_FAILED,
        IngestionStatus.SUCCESS,
        IngestionStatus.SUCCESS,
    ]
    assert [r.attachment_id for r in results] == ["att-bad", "att-ok", None]


# --- 빈 extracted_text 위임 (코드 리뷰 P1-3) ---


def test_attachment_empty_text_with_download_url_delegates_to_file_extraction() -> None:
    """P1-3 — 빈 extracted_text + 파일 원천(download_url) → LOW_QUALITY 가 아니라 청킹 위임."""
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_attachment(_attachment("att-1", page_id="page-1", text=""))
    captured: list[Attachment] = []

    def _capture(attachment: Attachment, page: PageObject, attachment_type: object) -> list[Chunk]:
        captured.append(attachment)
        return _fake_chunk_attachment(attachment, page, AttachmentType.PDF)

    deps = _att_deps(raw, chunk_fn=_capture)

    result = process_chunking_message(_ATT_MSG, deps)

    # 종전 LOW_QUALITY_ATTACH 스킵이 아니라 파일 기반 추출(chunk_attachment)로 진행된다.
    assert result.status is IngestionStatus.SUCCESS
    assert result.chunks == 1
    assert captured and captured[0].attachment_id == "att-1"


def test_attachment_empty_text_without_source_still_low_quality() -> None:
    """P1-3 — 빈 extracted_text + 파일 원천 없음(download_url/local_path 빈 값)은 여전히 차단."""
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_attachment(_attachment("att-1", page_id="page-1", text="", download_url=""))
    jobs = FakeIngestionJobsRepository()
    deps = _att_deps(raw, jobs=jobs)

    result = process_chunking_message(_ATT_MSG, deps)

    assert result.status is IngestionStatus.LOW_QUALITY_ATTACH
    assert jobs.records[0].stage is IngestionStage.ANALYZE
    assert jobs.records[0].status is IngestionStatus.LOW_QUALITY_ATTACH


def test_attachment_reindex_same_version_is_idempotent_skip() -> None:
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_attachment(_attachment("att-1", page_id="page-1"))
    cache = FakeEmbeddingCache()

    first = process_chunking_message(_ATT_MSG, _att_deps(raw, cache=cache))
    assert first.upserted == 1

    second = process_chunking_message(_ATT_MSG, _att_deps(raw, cache=cache))
    assert second.status is IngestionStatus.SUCCESS
    assert second.upserted == 0
    assert second.skipped == first.upserted


def test_missing_raw_attachment_raises() -> None:
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))  # 부모 페이지는 있지만 첨부는 없음.
    deps = _att_deps(raw)
    try:
        process_chunking_message(_ATT_MSG, deps)
    except AttachmentNotFoundError as exc:
        assert "att-1" in str(exc)
    else:  # pragma: no cover - 실패 시에만
        raise AssertionError("AttachmentNotFoundError expected")


def test_attachment_message_missing_parent_page_raises() -> None:
    raw = FakeRawPageStore()
    raw.save_attachment(_attachment("att-1", page_id="ghost"))  # 부모 페이지 없음.
    deps = _att_deps(raw)
    try:
        process_chunking_message(
            {"page_id": "ghost", "attachment_id": "att-1", "source_type": "attachment"}, deps
        )
    except RawPageNotFoundError as exc:
        assert "ghost" in str(exc)
    else:  # pragma: no cover - 실패 시에만
        raise AssertionError("RawPageNotFoundError expected")


def test_run_worker_dispatches_page_and_attachment_messages() -> None:
    raw = FakeRawPageStore()
    raw.save_page(_page("page-1"))
    raw.save_attachment(_attachment("att-1", page_id="page-1"))
    consumer = FakeMessageConsumer(
        messages=[
            {"page_id": "page-1"},  # source_type 미지정 → 본문 경로(기본 page).
            {
                "page_id": "page-1",
                "attachment_id": "att-1",
                "source_type": SourceType.ATTACHMENT.value,
            },
        ]
    )
    deps = _att_deps(raw)

    results = run_chunking_worker(consumer, deps)

    assert [r.attachment_id for r in results] == [None, "att-1"]
    assert all(r.status is IngestionStatus.SUCCESS for r in results)
