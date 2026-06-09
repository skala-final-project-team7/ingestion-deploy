"""Document Source Adapter 추상 인터페이스.

--------------------------------------------------
작성자 : 최태성
작성목적 : RAG 파이프라인이 데이터 공급원에 직접 결합하지 않도록, 공급원이 무엇이든
          동일한 표준 PageObject 스트림을 반환하도록 강제하는 추상 인터페이스.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature2 — DocumentSourceAdapter / ActiveIds / ChangeEvent 정의
--------------------------------------------------
[호환성]
  - Python 3.11.x, Pydantic 2.7+
--------------------------------------------------
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.page_object import PageObject


class ActiveIds(BaseModel):
    """공급원에 현재 살아있는 페이지·첨부 ID 집합 — Reconciliation 대조용 (설계서 §3.7)."""

    pages: set[str] = Field(default_factory=set)
    attachments: set[str] = Field(default_factory=set)


class ChangeEvent(BaseModel):
    """실시간 변경 이벤트 — watch_changes 스트림의 단위."""

    event_type: str  # created | updated | deleted
    page_id: str
    attachment_id: str | None = None


class DocumentSourceAdapter(ABC):
    """데이터 공급원 추상 인터페이스 (설계서 §4 / docs/atlassian-api.md).

    공급원(JSON 픽스처 / Atlassian / ...)이 무엇이든 동일한 표준 PageObject
    스트림을 반환하도록 강제한다. 파이프라인 본체는 어떤 어댑터인지 알지 못한다.
    """

    @abstractmethod
    def fetch_pages(self, since: datetime | None = None) -> Iterator[PageObject]:
        """페이지를 표준 PageObject 스트림으로 반환한다.

        Args:
            since: 지정 시 last_modified가 since 이후인 페이지만 반환(증분 동기화).
                None이면 전체(Full Crawl).
        """

    @abstractmethod
    def list_active_ids(self) -> ActiveIds:
        """공급원에 현재 살아있는 페이지·첨부 ID 집합 (Reconciliation용)."""

    @abstractmethod
    def watch_changes(self) -> Iterator[ChangeEvent]:
        """실시간 변경 이벤트 스트림. 공급원이 미지원하면 빈 스트림을 반환한다."""
