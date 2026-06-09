"""FakeQdrantPoolStore soft-delete 검증 (ADR 0003 항목 4).

실 ``QdrantPoolStore`` 와 인터페이스 정합으로 추가한 soft_delete_by_page_id /
soft_delete_by_attachment_id 가 Point 를 보존한 채 ``is_deleted`` 플래그만 갱신하는지
검증한다(hard delete 와 구분). 외부 Qdrant 없이 in-memory Fake 로 수행.
"""

from __future__ import annotations

from datetime import datetime

from app.ingestion.embedder.base import SparseVector
from app.ingestion.vector_store import CONTENT_POOL
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


def _upsert(store: FakeQdrantPoolStore, chunk: Chunk) -> None:
    store.upsert_chunks_batch(CONTENT_POOL, [(chunk, 1, [], _EMPTY_SPARSE)])


def test_soft_delete_by_page_id_sets_flag_and_preserves_point() -> None:
    store = FakeQdrantPoolStore()
    _upsert(store, _chunk(chunk_id="a" * 40, page_id="P1"))
    _upsert(store, _chunk(chunk_id="b" * 40, page_id="P2"))

    store.soft_delete_by_page_id("P2")

    pool = store.points[CONTENT_POOL]
    # 두 Point 모두 보존(hard delete 와 차이) — P2 만 is_deleted=True.
    assert set(pool) == {"a" * 40, "b" * 40}
    assert pool["a" * 40].is_deleted is False
    assert pool["b" * 40].is_deleted is True


def test_soft_delete_by_attachment_id_sets_flag() -> None:
    store = FakeQdrantPoolStore()
    _upsert(store, _chunk(chunk_id="a" * 40, page_id="P1"))
    _upsert(store, _chunk(chunk_id="b" * 40, page_id="P1", attachment_id="ATT-1"))

    store.soft_delete_by_attachment_id("ATT-1")

    pool = store.points[CONTENT_POOL]
    assert pool["a" * 40].is_deleted is False
    assert pool["b" * 40].is_deleted is True
