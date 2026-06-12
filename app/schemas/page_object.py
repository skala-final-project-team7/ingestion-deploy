"""Ingestion 입력 표준 — PageObject / Attachment.

--------------------------------------------------
작성자 : 최태성
담당 영역 : ingestion
작성목적 : Document Source Adapter가 반환하는 표준 PageObject와 첨부 객체를
          정의한다. 공급원(JSON 픽스처 / Atlassian)과 무관하게 동결되는 계약.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature1 schemas — 설계서 §7.1 PageObject 스펙 구현
  - 2026-05-17, 코드 리뷰 후속(P1-3) — Attachment.local_path 추가(비파괴): download_url은
    사용자 노출용 URL 의미로 동결, 청커가 파일을 직접 열 때는 local_path를 우선 사용
    (ADR-2026-001). 운영 어댑터는 local_path를 비워두고 다운로드 헬퍼로 채운다.
  - 2026-06-10, A8 잔여 — space_id/space_name 필드 추가(출처 카드 spaceId/spaceName 원천).
--------------------------------------------------
[호환성]
  - Python 3.11.x, Pydantic 2.7+
--------------------------------------------------
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.enums import ExtractedFormat


class Attachment(BaseModel):
    """페이지에 부속된 첨부 파일. 텍스트 추출 결과를 포함한다 (설계서 §3.2).

    Note:
        ``download_url``은 사용자에게 노출 가능한 URL(또는 URI) 의미로 동결한다.
        청커가 첨부 파일을 파일 시스템에서 직접 열어 텍스트를 추출해야 할 때는
        ``local_path``를 우선 사용한다. 운영(Atlassian) 어댑터는 ``local_path``를
        비워두고, 청커는 별도 다운로드 헬퍼로 임시 경로를 채운 뒤 호출한다.
        결정 근거: 코드리뷰 P1-3 (2026-05-17) — download_url(노출용)/local_path(파일 경로) 분리.
    """

    attachment_id: str
    filename: str
    mime_type: str
    extracted_text: str
    extracted_format: ExtractedFormat
    download_url: str
    parent_page_id: str
    last_modified: datetime
    file_size_bytes: int | None = None
    # 청커가 파일 시스템에서 직접 열기 위한 로컬 경로 (PoC: JsonFixtureSourceAdapter가 채움).
    # 운영 어댑터는 비워두고, 다운로드 단계에서 채워진다.
    local_path: str | None = None


class PageObject(BaseModel):
    """RAG 파이프라인이 수신하는 표준 페이지 객체 (설계서 §7.1).

    공급원 전환(JSON 픽스처 ↔ Atlassian)에도 본 스펙은 변경되지 않는다.
    ``allowed_groups``/``allowed_users``는 필수 필드이나 빈 배열이 허용되며,
    둘 다 비어 있으면 ``is_acl_missing``으로 식별하여 Ingestion 단계에서
    ``INVALID_ACL``로 처리한다 (스키마 단에서 거부하지 않음).
    """

    page_id: str
    space_key: str
    title: str
    body_html: str
    version_number: int
    last_modified: datetime
    allowed_groups: list[str]
    allowed_users: list[str]
    webui_link: str
    labels: list[str] = Field(default_factory=list)
    ancestors: list[str] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    # 2026-06-10(A8 잔여) — 출처 카드 sources[].spaceId/spaceName 원천. 크롤 payload 의
    # SpaceInfo(id/name)에서 채우며, 원천이 없는 공급원(fixture 의 id 등)은 빈 문자열 허용.
    space_id: str = ""
    space_name: str = ""

    @property
    def is_acl_missing(self) -> bool:
        """allowed_groups·allowed_users가 모두 비면 ACL 누락(INVALID_ACL 대상)."""
        return not self.allowed_groups and not self.allowed_users
