"""청크 및 청크 메타데이터 — Chunk / ChunkMetadata.

--------------------------------------------------
작성자 : 최태성
작성목적 : Adaptive Chunker가 산출하는 청크와 메타데이터 21종을 정의하고,
          멱등 upsert의 전제인 결정론적 chunk_id 생성 함수를 제공한다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature1 schemas — chunking-strategy.md §6 정합
  - 2026-05-17, 코드 리뷰 후속(P2) — doc_type을 ``DocType | AttachmentType``으로 정적
    강제(StrEnum이라 직렬화 의미는 동일, 잘못된 값 주입을 컴파일 시 차단)
  - 2026-06-10, 코드 리뷰 재점검(P4) — '첨부 전용 5종' 라벨을 'source_type 구분자 +
    첨부 전용 4종' 으로 정정(필드 수 불일치 해소). rag 레포와 미러.
  - 2026-06-10, A8 잔여 — space_id/space_name 추가(공통 15종, 21종 체계 —
    sources[].spaceId/spaceName 원천). 구버전 payload 호환 위해 기본 빈 문자열.
--------------------------------------------------
[호환성]
  - Python 3.11.x, Pydantic 2.7+
--------------------------------------------------
"""

import hashlib
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.enums import AttachmentType, DocType, ExtractedFormat, SourceType


def make_chunk_id(page_id: str, chunk_index: int, attachment_id: str | None = None) -> str:
    """결정론적 chunk_id 생성 — SHA1(page_id + chunk_index + attachment_id).

    동일 (page_id, chunk_index, attachment_id) → 동일 chunk_id를 보장하여
    재색인 시 멱등 upsert가 성립한다. 임의 UUID 사용 금지 (chunking-strategy.md §6.1).

    Args:
        page_id: 부모 페이지 page_id. 첨부 청크도 부모 page_id를 사용한다.
        chunk_index: 동일 페이지/첨부 내 0-based 순번.
        attachment_id: 첨부 청크의 attachment_id. 본문 청크는 None.

    Returns:
        40자리 SHA1 16진수 문자열.
    """
    raw = f"{page_id}:{chunk_index}:{attachment_id or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class ChunkMetadata(BaseModel):
    """청크 메타데이터 21종 — 공통 15 + 첨부 5 + 검증 1 (chunking-strategy.md §6).

    첨부 전용 4종(attachment_id/filename/mime, extracted_format)과 구분자 source_type 중
    첨부 4종은 본문 청크에서 None이며, source_type은 모든 청크가 명시한다.
    """

    # --- 공통 15종 ---
    chunk_id: str
    page_id: str
    page_title: str
    section_header: str
    section_path: str
    chunk_index: int
    labels: list[str] = Field(default_factory=list)
    # 본문은 DocType, 첨부는 AttachmentType. StrEnum이라 직렬화는 동일한 소문자 문자열.
    doc_type: DocType | AttachmentType
    space_key: str
    # 2026-06-10(A8 잔여) — sources[].spaceId/spaceName 원천(BFF 영속 필드). 기존 색인
    # payload 호환을 위해 기본 빈 문자열(검색 복원 시 .get 폴백 — search_node).
    space_id: str = ""
    space_name: str = ""
    allowed_groups: list[str]
    allowed_users: list[str]
    webui_link: str
    last_modified: datetime
    # --- source_type 구분자 + 첨부 전용 4종 ---
    source_type: SourceType
    attachment_id: str | None = None
    attachment_filename: str | None = None
    attachment_mime: str | None = None
    extracted_format: ExtractedFormat | None = None
    # --- 검증용 1종 ---
    token_count: int


class Chunk(BaseModel):
    """임베딩·검색 단위. 텍스트 본문과 메타데이터로 구성된다."""

    text: str
    metadata: ChunkMetadata
