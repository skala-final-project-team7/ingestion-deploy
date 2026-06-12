"""Raw Store — MongoDB ``raw_pages`` / ``raw_attachments`` 적재 어댑터 [Storage].

--------------------------------------------------
작성자 : 최태성
담당 영역 : ingestion
작성목적 : Data Ingestion Agent(FR-001)가 수집한 표준 PageObject·첨부 원본을 MongoDB
          ``raw_pages`` / ``raw_attachments`` 컬렉션에 적재하기 위한 어댑터. 후속
          Chunking/Extraction Worker 가 ``page_id`` / ``attachment_id`` 로 원본을
          조회한다. ``jobs.py`` / ``mongo_cache.py`` 의 ABC + Fake + Mongo 3계층 패턴을
          재사용해 일관성을 유지한다(`app/CLAUDE.md` §8 — 외부 호출은 어댑터로 분리).
작성일 : 2026-05-26
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-26, 최초 작성, featureI-6 — RawPageStore ABC + FakeRawPageStore +
    MongoRawPageStore. page_id/attachment_id 기준 멱등 upsert(재크롤 안전).
  - 2026-05-26, featureI-3b — get_attachment(attachment_id) 읽기 메서드 추가. 첨부
    Chunking Worker 가 메시지의 attachment_id 로 raw_attachments 원본을 조회한다
    (메시지엔 식별자만 싣고 첨부 메타·extracted_text 는 raw_attachments 에서 로드).
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - pymongo>=4.7 (MongoRawPageStore 가 사용)
  - 외부 의존성 0 (base ABC + FakeRawPageStore 는 pymongo 미설치 환경에서도 동작)
--------------------------------------------------
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.config import Settings
from app.schemas.page_object import Attachment, PageObject


class RawPageStore(ABC):
    """``raw_pages`` / ``raw_attachments`` 적재 추상 인터페이스 — crawler 가 호출한다.

    조회 API 는 Chunking/Extraction Worker 도입 시 별도로 분리한다 — 본 ABC 는 적재만
    책임진다(오버튜닝 회피, jobs.py 와 동일 정책).
    """

    @abstractmethod
    def save_page(self, page: PageObject) -> None:
        """PageObject 1건을 ``raw_pages`` 에 멱등 upsert 한다(키: ``page_id``)."""

    @abstractmethod
    def save_attachment(self, attachment: Attachment) -> None:
        """Attachment 1건을 ``raw_attachments`` 에 멱등 upsert 한다(키: ``attachment_id``)."""

    @abstractmethod
    def get_page(self, page_id: str) -> PageObject | None:
        """``page_id`` 의 ``raw_pages`` 원본을 PageObject 로 복원한다. 없으면 None.

        Chunking Worker 가 ``content.chunking`` 메시지의 ``page_id`` 로 본문 원본을 조회할 때
        사용한다(메시지에는 식별자만 싣고 본문은 raw_pages 에서 로드).
        """

    @abstractmethod
    def get_attachment(self, attachment_id: str) -> Attachment | None:
        """``attachment_id`` 의 ``raw_attachments`` 원본을 Attachment 로 복원한다. 없으면 None.

        첨부 Chunking Worker 가 ``content.chunking``(``source_type=attachment``) 메시지의
        ``attachment_id`` 로 첨부 메타·``extracted_text``·다운로드 핸들을 조회할 때 사용한다
        (메시지엔 식별자만 싣고 첨부 본문은 raw_attachments 에서 로드 — featureI-3b).
        """


@dataclass(slots=True)
class FakeRawPageStore(RawPageStore):
    """In-memory ``RawPageStore`` — 테스트·PoC 용(외부 의존성 0).

    ``pages`` / ``attachments`` 는 ``page_id`` / ``attachment_id`` 키 dict 로 멱등 동작을
    그대로 재현한다(같은 id 재적재 시 덮어쓰기). 테스트가 적재 상태를 직접 확인한다.
    """

    pages: dict[str, PageObject] = field(default_factory=dict)
    attachments: dict[str, Attachment] = field(default_factory=dict)

    def save_page(self, page: PageObject) -> None:
        self.pages[page.page_id] = page

    def save_attachment(self, attachment: Attachment) -> None:
        self.attachments[attachment.attachment_id] = attachment

    def get_page(self, page_id: str) -> PageObject | None:
        return self.pages.get(page_id)

    def get_attachment(self, attachment_id: str) -> Attachment | None:
        return self.attachments.get(attachment_id)


class MongoRawPageStore(RawPageStore):
    """MongoDB ``raw_pages`` / ``raw_attachments`` 컬렉션 클라이언트 — db-schema §2.6/§2.7.

    Args:
        client: 사전 구성된 pymongo MongoClient. ``from_settings`` 가 환경 설정에서
            생성하거나, 테스트가 mock 객체를 주입한다.
        db_name: 데이터베이스 이름 (Settings.mongo_db 기본값 ``lina_rag``).
        pages_collection / attachments_collection: 컬렉션 이름.

    Raises:
        ImportError: pymongo 미설치 시 ``from_settings`` 호출 단계에서 발생.
    """

    def __init__(
        self,
        client: object,
        db_name: str,
        *,
        pages_collection: str = "raw_pages",
        attachments_collection: str = "raw_attachments",
    ) -> None:
        # pymongo MongoClient 는 dict-style 인덱싱으로 DB·컬렉션을 얻는다(기존 어댑터 패턴).
        self._pages = client[db_name][pages_collection]  # type: ignore[index]
        self._attachments = client[db_name][attachments_collection]  # type: ignore[index]

    @classmethod
    def from_settings(cls, settings: Settings) -> MongoRawPageStore:
        """환경 설정에서 MongoClient 를 생성해 인스턴스화한다(운영 경로)."""
        from pymongo import MongoClient

        client: MongoClient = MongoClient(settings.mongo_uri)  # type: ignore[type-arg]
        return cls(client=client, db_name=settings.mongo_db)

    def save_page(self, page: PageObject) -> None:
        # 멱등 upsert: 같은 page_id 가 있으면 덮어쓴다(재크롤·Delta 재투입 안전).
        self._pages.update_one(  # type: ignore[attr-defined]
            {"page_id": page.page_id},
            {"$set": page.model_dump(mode="json")},
            upsert=True,
        )

    def save_attachment(self, attachment: Attachment) -> None:
        self._attachments.update_one(  # type: ignore[attr-defined]
            {"attachment_id": attachment.attachment_id},
            {"$set": attachment.model_dump(mode="json")},
            upsert=True,
        )

    def get_page(self, page_id: str) -> PageObject | None:
        doc = self._pages.find_one(  # type: ignore[attr-defined]
            {"page_id": page_id}, projection={"_id": 0}
        )
        if doc is None:
            return None
        return PageObject.model_validate(doc)

    def get_attachment(self, attachment_id: str) -> Attachment | None:
        doc = self._attachments.find_one(  # type: ignore[attr-defined]
            {"attachment_id": attachment_id}, projection={"_id": 0}
        )
        if doc is None:
            return None
        return Attachment.model_validate(doc)
