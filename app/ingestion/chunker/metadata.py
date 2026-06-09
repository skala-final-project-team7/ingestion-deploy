"""청크 메타데이터 부착.

--------------------------------------------------
작성자 : 최태성
작성목적 : 1차 분할·크기 규칙을 거친 ChunkDraft에 청크 메타데이터 19종을 부착해
          ChunkMetadata를 생성한다 (chunking-strategy.md §6).
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature3-B — build_metadata (본문 청크)
  - 2026-05-17, 코드 리뷰 후속(P2) — doc_type을 DocType enum 그대로 전달
    (ChunkMetadata.doc_type 정적 강제 반영)
--------------------------------------------------
[호환성]
  - Python 3.11.x, Pydantic 2.7+
--------------------------------------------------
"""

from app.ingestion.chunker.base import ChunkDraft
from app.ingestion.chunker.tokenizer import count_tokens
from app.schemas.chunk import ChunkMetadata, make_chunk_id
from app.schemas.enums import DocType, SourceType
from app.schemas.page_object import PageObject


def build_metadata(
    page: PageObject,
    draft: ChunkDraft,
    chunk_index: int,
    doc_type: DocType,
) -> ChunkMetadata:
    """본문 ChunkDraft에 청크 메타데이터 19종을 부착한다.

    무결성 규칙(chunking-strategy.md §6.3):
    - section_header 빈 문자열 금지 → 'untitled'
    - chunk_id 결정론적 계산 (make_chunk_id)
    - 본문 청크는 source_type=page, 첨부 전용 필드(attachment_*, extracted_format)는 None

    Args:
        page: 청크의 출처 PageObject.
        draft: 1차 분할·크기 규칙을 거친 ChunkDraft.
        chunk_index: 동일 페이지 내 0-based 순번.
        doc_type: 본문 문서 유형.

    Returns:
        19종 필드가 채워진 ChunkMetadata.
    """
    section_header = draft.section_header.strip() or "untitled"
    section_path = " > ".join([*page.ancestors, section_header])
    return ChunkMetadata(
        chunk_id=make_chunk_id(page.page_id, chunk_index),
        page_id=page.page_id,
        page_title=page.title,
        section_header=section_header,
        section_path=section_path,
        chunk_index=chunk_index,
        labels=page.labels,
        doc_type=doc_type,
        space_key=page.space_key,
        allowed_groups=page.allowed_groups,
        allowed_users=page.allowed_users,
        webui_link=page.webui_link,
        last_modified=page.last_modified,
        source_type=SourceType.PAGE,
        token_count=count_tokens(draft.text),
    )
