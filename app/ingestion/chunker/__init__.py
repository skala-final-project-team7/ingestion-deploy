"""app.ingestion.chunker — Adaptive Chunker [Pipeline].

doc_type / attachment_type 분기에 따라 본문·첨부 텍스트를 청크로 분할한다.
2단계 하이브리드: 1차 논리 단위 분할 → 2차 800토큰 재분할(100토큰 오버랩) → 200토큰 하한선 병합.
원자성 유지 유형(FAQ Q&A·ADR·회의록 안건·트러블슈팅 케이스)은 2차 분할·하한선 병합에서 제외한다.
상세 규칙: docs/chunking-strategy.md.

엔트리: chunk_page(page, doc_type=None) → list[Chunk]

구현 상태:
- tokenizer.py        count_tokens — 토큰 카운터 (PoC 휴리스틱) [feature3-A]
- storage_format.py   clean_storage_format — Storage Format(HTML) 전처리 [feature3-A]
- base.py             ChunkDraft / 2단계 분할(split/merge/apply_size_rules) [feature3-A]
- body.py             본문 6유형 1차 분할 + split_body + chunk_page + infer_doc_type [feature3-B]
- metadata.py         build_metadata — 본문 청크 메타데이터 21종 부착 [feature3-B]
- attachment.py       첨부 docx/xlsx/pdf/csv 분할 + chunk_attachment + infer_attachment_type
                      [feature4-A·4-B] (PDF/CSV = feature4-B 완료)
"""

from app.ingestion.chunker.attachment import (
    build_attachment_metadata,
    chunk_attachment,
    infer_attachment_type,
    split_attachment,
)
from app.ingestion.chunker.base import (
    MAX_TOKENS,
    MIN_TOKENS,
    OVERLAP_TOKENS,
    ChunkDraft,
    apply_size_rules,
    merge_undersized,
    split_oversized,
)
from app.ingestion.chunker.body import chunk_page, infer_doc_type, split_body
from app.ingestion.chunker.metadata import build_metadata
from app.ingestion.chunker.storage_format import clean_storage_format
from app.ingestion.chunker.tokenizer import count_tokens

__all__ = [
    "MAX_TOKENS",
    "MIN_TOKENS",
    "OVERLAP_TOKENS",
    "ChunkDraft",
    "apply_size_rules",
    "build_attachment_metadata",
    "build_metadata",
    "chunk_attachment",
    "chunk_page",
    "clean_storage_format",
    "count_tokens",
    "infer_attachment_type",
    "infer_doc_type",
    "merge_undersized",
    "split_attachment",
    "split_body",
    "split_oversized",
]
