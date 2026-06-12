"""SyncWorker 단위 테스트 — 3중 삭제 트리거 → soft_delete (featureI-5b).

작성자 : 최태성
담당 영역 : ingestion

Delta 확인 게이트(기본 미적용 / confirm 적용), Trash sync(FakeTrashSource), 실시간 Webhook
(page/attachment/빈 이벤트)를 검증한다. store 는 실 ``FakeQdrantPoolStore`` 를 써서 Point 가
보존된 채 ``is_deleted`` 만 set 되는지 끝까지 확인한다(funnel→store 정합).
"""

from __future__ import annotations

from datetime import datetime

from app.adapters.confluence_trash import FakeTrashSource, TrashedIds
from app.ingestion.embedder.base import SparseVector
from app.ingestion.sync import DeltaSyncResult
from app.ingestion.vector_store import CONTENT_POOL
from app.ingestion.workers.sync_worker import (
    SyncWorker,
    SyncWorkerDeps,
    WebhookDeleteEvent,
)
from app.schemas.chunk import Chunk, ChunkMetadata
from app.schemas.enums import DocType, SourceType
from app.storage.qdrant_fake import FakeQdrantPoolStore

_EMPTY_SPARSE = SparseVector(indices=(), values=())


def _chunk(*, chunk_id: str, page_id: str, attachment_id: str | None = None) -> Chunk:
    metadata = ChunkMetadata(
        chunk_id=chunk_id,
        page_id=page_id,
        page_title="T",
        section_header="H",
        section_path="H",
        chunk_index=0,
        labels=[],
        doc_type=DocType.OPERATION,
        space_key="CLOUD",
        allowed_groups=["space:CLOUD"],
        allowed_users=[],
        webui_link="/x",
        last_modified=datetime.fromisoformat("2026-05-14T01:00:00+00:00"),
        source_type=SourceType.PAGE if attachment_id is None else SourceType.ATTACHMENT,
        attachment_id=attachment_id,
        token_count=10,
    )
    return Chunk(text="body", metadata=metadata)


def _seeded_store() -> FakeQdrantPoolStore:
    """P1(본문), P2(본문), P3+ATT-1(첨부) 청크를 적재한 store."""
    store = FakeQdrantPoolStore()
    store.upsert_chunks_batch(
        CONTENT_POOL, [(_chunk(chunk_id="a" * 40, page_id="P1"), 1, [], _EMPTY_SPARSE)]
    )
    store.upsert_chunks_batch(
        CONTENT_POOL, [(_chunk(chunk_id="b" * 40, page_id="P2"), 1, [], _EMPTY_SPARSE)]
    )
    store.upsert_chunks_batch(
        CONTENT_POOL,
        [(_chunk(chunk_id="c" * 40, page_id="P3", attachment_id="ATT-1"), 1, [], _EMPTY_SPARSE)],
    )
    return store


def _flag(store: FakeQdrantPoolStore, chunk_id: str) -> bool:
    return store.points[CONTENT_POOL][chunk_id].is_deleted


def test_apply_delta_deletions_gate_default_does_not_apply() -> None:
    store = _seeded_store()
    worker = SyncWorker(SyncWorkerDeps(store=store))

    result = worker.apply_delta_deletions(DeltaSyncResult(deleted_candidate_page_ids=["P2"]))

    # 확인 게이트(기본 confirm=False): 아무것도 삭제하지 않음 — 기존 surface-only 정책 보존.
    assert result.total_soft_deleted == 0
    assert _flag(store, "b" * 40) is False


def test_apply_delta_deletions_confirm_soft_deletes_candidates() -> None:
    store = _seeded_store()
    worker = SyncWorker(SyncWorkerDeps(store=store))

    result = worker.apply_delta_deletions(
        DeltaSyncResult(deleted_candidate_page_ids=["P2"]),
        confirm=True,
    )

    assert result.soft_deleted_page_ids == ["P2"]
    assert _flag(store, "b" * 40) is True
    assert _flag(store, "a" * 40) is False  # P1 무관 — 보존


def test_run_trash_sync_soft_deletes_trashed_pages_and_attachments() -> None:
    store = _seeded_store()
    trash = FakeTrashSource(TrashedIds(pages={"P1"}, attachments={"ATT-1"}))
    worker = SyncWorker(SyncWorkerDeps(store=store, trash_source=trash))

    result = worker.run_trash_sync()

    assert result.soft_deleted_page_ids == ["P1"]
    assert result.soft_deleted_attachment_ids == ["ATT-1"]
    assert _flag(store, "a" * 40) is True  # P1 본문
    assert _flag(store, "c" * 40) is True  # ATT-1 첨부
    assert _flag(store, "b" * 40) is False  # P2 무관


def test_run_trash_sync_without_source_is_noop() -> None:
    store = _seeded_store()
    worker = SyncWorker(SyncWorkerDeps(store=store, trash_source=None))

    result = worker.run_trash_sync()

    assert result.total_soft_deleted == 0
    assert _flag(store, "a" * 40) is False


def test_handle_webhook_event_page_and_attachment() -> None:
    store = _seeded_store()
    worker = SyncWorker(SyncWorkerDeps(store=store))

    page_result = worker.handle_webhook_event(WebhookDeleteEvent(page_id="P2"))
    att_result = worker.handle_webhook_event(WebhookDeleteEvent(attachment_id="ATT-1"))

    assert page_result.soft_deleted_page_ids == ["P2"]
    assert att_result.soft_deleted_attachment_ids == ["ATT-1"]
    assert _flag(store, "b" * 40) is True
    assert _flag(store, "c" * 40) is True


def test_handle_webhook_event_empty_is_noop() -> None:
    store = _seeded_store()
    worker = SyncWorker(SyncWorkerDeps(store=store))

    result = worker.handle_webhook_event(WebhookDeleteEvent())

    assert result.total_soft_deleted == 0
    assert _flag(store, "a" * 40) is False
