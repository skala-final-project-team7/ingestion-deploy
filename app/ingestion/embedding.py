"""Dual Embedding — Pool별 임베딩 입력 텍스트 구성 + 멱등성 판정 [Pipeline].

--------------------------------------------------
작성자 : 최태성
작성목적 : LINA RAG 파이프라인의 Dual Embedding 단계에서, 청크를 Pool별 임베딩 입력
          텍스트로 변환하고 embedding_cache 기반 재임베딩 멱등성을 판정한다
          (rag-pipeline-design.md §5, db-schema.md §1·§2.4, app/CLAUDE.md §4).
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature5-A — pool_embedding_texts / should_skip_embedding
    (순수 로직)
  - 2026-05-17, 코드 리뷰 후속(P2) — metadata.doc_type이 enum이 된 후에도 동일 텍스트로
    join되도록 .value 명시
--------------------------------------------------
[호환성]
  - Python 3.11.x, Pydantic 2.7+
  - NOTE: 실제 Dense(multilingual-e5-large)·Sparse(BM25) 임베딩과 MongoDB
          embedding_cache I/O는 feature5-B(클라이언트 연동) 책임이다. e5 모델의
          'passage:' 프리픽스 등 모델별 처리도 feature5-B가 적용한다.
--------------------------------------------------
"""

from app.ingestion.vector_store import CONTENT_POOL, LABEL_POOL, TITLE_POOL
from app.schemas.chunk import Chunk
from app.schemas.enums import SourceType


def pool_embedding_texts(chunk: Chunk) -> dict[str, str]:
    """청크를 Multi-Pool 임베딩 입력 텍스트로 변환한다 (db-schema.md §1).

    - title_pool: `page_title + section_header` (첨부 청크는 `attachment_filename +
      section_header`)
    - content_pool: 청크 본문 텍스트
    - label_pool: `labels + space_key + doc_type` 결합 짧은 텍스트

    Args:
        chunk: 임베딩 대상 청크.

    Returns:
        Pool 이름 → 그 Pool에 임베딩할 입력 텍스트.
    """
    metadata = chunk.metadata
    if metadata.source_type is SourceType.ATTACHMENT and metadata.attachment_filename:
        title_text = f"{metadata.attachment_filename} {metadata.section_header}"
    else:
        title_text = f"{metadata.page_title} {metadata.section_header}"
    label_text = " ".join([*metadata.labels, metadata.space_key, metadata.doc_type.value])
    return {
        TITLE_POOL: title_text.strip(),
        CONTENT_POOL: chunk.text,
        LABEL_POOL: label_text.strip(),
    }


def should_skip_embedding(version_number: int, cached_version: int | None) -> bool:
    """재임베딩·재upsert를 건너뛸지 판정한다 (app/CLAUDE.md §4 — 멱등성).

    embedding_cache에 동일 chunk_id 항목이 있고 그 version_number가 현재와 같으면
    재임베딩을 스킵한다. 캐시에 없거나(None) 버전이 다르면 재임베딩이 필요하다.

    Args:
        version_number: 현재 청크 부모 페이지의 버전.
        cached_version: embedding_cache에 기록된 버전. 캐시에 없으면 None.

    Returns:
        재임베딩을 건너뛰어도 되면 True.
    """
    return cached_version == version_number
