"""In-memory Fake Qdrant Multi-Pool Store — PoC·테스트용 [Storage].

--------------------------------------------------
작성자 : 최태성
작성목적 : ``QdrantPoolStore`` 의 적재(upsert)·삭제 동기화(scroll/delete) 인터페이스를 외부
          Qdrant 서버 없이 in-memory 로 재현하는 Fake. Ingestion indexer 의 멱등 upsert 와
          삭제 동기화(reconcile_deletions)의 scroll/delete 를 PoC·단위 테스트에서 구동하기
          위한 drop-in 이다. 공유 자산 ``qdrant_client.py`` 는 수정하지 않고(additive) 별도
          모듈로 둔다(`app/CLAUDE.md` §8 — 테스트 대체 구현).
작성일 : 2026-05-26 (featureI-7)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-26, 최초 작성, featureI-7 — FakeQdrantPoolStore(upsert/scroll/delete) in-memory.
  - 2026-05-26, ADR 0003 항목 4 — soft_delete_by_page_id / soft_delete_by_attachment_id
    추가(실 store 와 인터페이스 정합). _StoredPoint 에 is_deleted 플래그 보존.
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - 외부 의존성 0 (qdrant-client 미설치 환경에서도 동작)
--------------------------------------------------
[범위] Ingestion 이 사용하는 메서드만 구현한다. 검색(search)은 Query 단계(별도 레포) 책임이라
       미구현(NotImplementedError) — 본 Fake 는 적재·삭제 동기화 검증 전용.
--------------------------------------------------
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from app.ingestion.embedder.base import SparseVector
from app.ingestion.vector_store import CONTENT_POOL, POOL_NAMES
from app.schemas.chunk import Chunk
from app.schemas.enums import SourceType


@dataclass(frozen=True, slots=True)
class _StoredPoint:
    """Fake 적재 단위 — 삭제 동기화·검증에 필요한 식별자만 보존."""

    chunk_id: str
    page_id: str
    attachment_id: str | None
    source_type: SourceType
    version_number: int
    is_deleted: bool = False


class FakeQdrantPoolStore:
    """In-memory Multi-Pool 저장소 — ``QdrantPoolStore`` 적재/삭제 인터페이스 호환.

    세 Pool(title/content/label)을 각각 ``chunk_id -> _StoredPoint`` dict 로 보존하며, 같은
    ``chunk_id`` 재적재는 덮어쓴다(멱등). 테스트는 ``points`` 를 직접 조회해 적재 상태를 확인한다.
    """

    def __init__(self) -> None:
        self.points: dict[str, dict[str, _StoredPoint]] = {pool: {} for pool in POOL_NAMES}

    def upsert_chunks_batch(
        self,
        pool_name: str,
        items: Iterable[tuple[Chunk, int, list[float], SparseVector]],
    ) -> None:
        """Pool 에 청크들을 멱등 적재한다(벡터는 보존하지 않고 식별자만 — 적재 검증 목적)."""
        pool = self.points[pool_name]
        for chunk, version, _dense, _sparse in items:
            metadata = chunk.metadata
            pool[metadata.chunk_id] = _StoredPoint(
                chunk_id=metadata.chunk_id,
                page_id=metadata.page_id,
                attachment_id=metadata.attachment_id,
                source_type=metadata.source_type,
                version_number=version,
            )

    def scroll_page_ids(self, *, batch_size: int = 1000) -> set[str]:
        """본문 청크의 ``page_id`` unique set — CONTENT_POOL 만 스캔(QdrantPoolStore 동일)."""
        return {
            point.page_id
            for point in self.points[CONTENT_POOL].values()
            if point.source_type is SourceType.PAGE
        }

    def scroll_attachment_ids(self, *, batch_size: int = 1000) -> set[str]:
        """첨부 청크의 ``attachment_id`` unique set — CONTENT_POOL 만 스캔."""
        return {
            point.attachment_id
            for point in self.points[CONTENT_POOL].values()
            if point.source_type is SourceType.ATTACHMENT and point.attachment_id is not None
        }

    def delete_by_page_id(self, page_id: str) -> None:
        """``page_id`` 가 일치하는 Point 를 세 Pool 에서 삭제한다(문서 단위 cascade)."""
        for pool in self.points.values():
            for chunk_id in [cid for cid, p in pool.items() if p.page_id == page_id]:
                del pool[chunk_id]

    def delete_by_attachment_id(self, attachment_id: str) -> None:
        """``attachment_id`` 가 일치하는 Point 를 세 Pool 에서 삭제한다."""
        for pool in self.points.values():
            for chunk_id in [cid for cid, p in pool.items() if p.attachment_id == attachment_id]:
                del pool[chunk_id]

    def delete_by_chunk_id(self, chunk_id: str) -> None:
        """``chunk_id`` Point 를 세 Pool 에서 삭제한다."""
        for pool in self.points.values():
            pool.pop(chunk_id, None)

    def soft_delete_by_page_id(self, page_id: str) -> None:
        """``page_id`` 일치 Point 의 ``is_deleted`` 를 True 로 설정한다 (소프트 삭제).

        실 ``QdrantPoolStore.soft_delete_by_page_id`` 와 동일 의미(ADR 0003 항목 4) —
        Point 는 보존하고 ``is_deleted`` 플래그만 갱신한다(hard delete 와 구분).
        """
        for pool in self.points.values():
            for cid, p in pool.items():
                if p.page_id == page_id:
                    pool[cid] = replace(p, is_deleted=True)

    def soft_delete_by_attachment_id(self, attachment_id: str) -> None:
        """``attachment_id`` 일치 Point 의 ``is_deleted`` 를 True 로 설정한다(소프트 삭제)."""
        for pool in self.points.values():
            for cid, p in pool.items():
                if p.attachment_id == attachment_id:
                    pool[cid] = replace(p, is_deleted=True)
