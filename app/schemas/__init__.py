"""app.schemas — 계층 간 데이터 계약 [Pipeline].

작성자 : 최태성
담당 영역 : ingestion

Ingestion 파이프라인 단계 간에 dict를 그대로 전달하지 않고 Pydantic 모델로 정의한다.
RAG 레포(skala-final/rag)와 공유하는 수집·청킹 모델만 둔다(질의/응답 모델 rag_state·response는
RAG 레포 전용이라 본 레포에는 포함하지 않는다).

- enums.py        DocType / AttachmentType / SourceType / ExtractedFormat /
                  IngestionStage / IngestionStatus / LlmModel
- page_object.py  PageObject, Attachment (Ingestion 입력 — 설계서 §7.1)
- chunk.py        Chunk, ChunkMetadata, make_chunk_id (chunking-strategy.md §6)
"""

from app.schemas.chunk import Chunk, ChunkMetadata, make_chunk_id
from app.schemas.enums import (
    AttachmentType,
    DocType,
    ExtractedFormat,
    IngestionStage,
    IngestionStatus,
    LlmModel,
    SourceType,
)
from app.schemas.page_object import Attachment, PageObject

__all__ = [
    "Attachment",
    "AttachmentType",
    "Chunk",
    "ChunkMetadata",
    "DocType",
    "ExtractedFormat",
    "IngestionStage",
    "IngestionStatus",
    "LlmModel",
    "PageObject",
    "SourceType",
    "make_chunk_id",
]
