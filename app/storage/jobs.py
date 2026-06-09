"""Ingestion Jobs Repository — ``ingestion_jobs`` 컬렉션 적재 어댑터 [Storage].

--------------------------------------------------
작성자 : 최태성
작성목적 : Ingestion 파이프라인 각 단계(analyze / chunk / embed / upsert / sync)의 처리
          결과를 MongoDB ``ingestion_jobs`` 컬렉션에 기록하기 위한 어댑터. 설계서 §3.1
          + db-schema §2.3 정합 (7필드: page_id / attachment_id / stage / status /
          started_at / finished_at / error). 관리자 대시보드 조회는 별도 시스템 책임이
          므로 본 어댑터는 적재(record / record_many) API 만 노출한다 (`app/CLAUDE.md`
          §8 — 외부 호출은 어댑터/클라이언트 계층으로 분리).
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature6 Phase 2 — IngestionJobRecord 값 객체 +
    IngestionJobsRepository ABC + FakeIngestionJobsRepository (in-memory) +
    MongoIngestionJobsRepository (insert_one + insert_many). chunk_lookup Phase 1
    패턴 재사용으로 일관성 유지.
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - pymongo>=4.7 (project main dependency — MongoIngestionJobsRepository가 사용)
  - 외부 의존성 0 (base ABC + FakeIngestionJobsRepository는 pymongo 미설치 환경에서도
    동작)
--------------------------------------------------
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from app.config import Settings
from app.schemas.enums import IngestionStage, IngestionStatus


@dataclass(frozen=True, slots=True)
class IngestionJobRecord:
    """``ingestion_jobs`` 단일 레코드 — db-schema §2.3 정합 (7필드).

    Attributes:
        page_id: 대상 페이지 식별자. 본문 잡과 첨부 잡 모두에 부모 페이지 id가 들어간다.
        attachment_id: 대상 첨부 식별자. 본문 잡은 ``None``.
        stage: 처리 단계 — ``analyze`` / ``chunk`` / ``embed`` / ``upsert`` / ``sync``.
        status: 처리 결과 — ``SUCCESS`` 또는 ``IngestionStatus`` 예외 코드 (설계서 §5.3
            + §8 정합).
        started_at: 단계 시작 시각.
        finished_at: 단계 종료 시각.
        error: ``status`` 가 ``SUCCESS`` 가 아닌 경우의 상세 사유 문구. ``SUCCESS``
            이면 ``None``.
    """

    page_id: str
    attachment_id: str | None
    stage: IngestionStage
    status: IngestionStatus
    started_at: datetime
    finished_at: datetime
    error: str | None


class IngestionJobsRepository(ABC):
    """``ingestion_jobs`` 적재 추상 인터페이스 — Ingestion 그래프 노드가 호출한다.

    조회 API(``list_recent`` / ``query`` 등)는 관리자 대시보드 도입 시 별도 milestone
    으로 분리한다 — 본 ABC 는 적재만 책임 (오버튜닝 회피).
    """

    @abstractmethod
    def record(self, record: IngestionJobRecord) -> None:
        """단일 레코드를 적재한다.

        Args:
            record: 적재할 레코드. 호출자(그래프 노드)가 단계 진입·종료 시점을 직접
                ``started_at`` / ``finished_at`` 에 채워 전달한다.
        """

    @abstractmethod
    def record_many(self, records: list[IngestionJobRecord]) -> None:
        """다수 레코드를 배치 적재한다.

        Args:
            records: 적재할 레코드 목록. 빈 입력은 noop 으로 처리한다 (운영은 pymongo
                ``insert_many`` 가 빈 docs 에서 ``InvalidOperation`` 을 발생시킴 —
                호출자에게 책임 떠넘기지 않도록 어댑터가 short-circuit).
        """


class FakeIngestionJobsRepository(IngestionJobsRepository):
    """In-memory IngestionJobsRepository — 테스트·PoC 용 (외부 의존성 0).

    호출 순서를 보존하는 list 로 적재하며, ``records`` 프로퍼티가 방어 복사본을 반환
    해 외부 변경이 내부 상태를 오염시키지 않도록 한다.
    """

    def __init__(self) -> None:
        self._records: list[IngestionJobRecord] = []

    @property
    def records(self) -> list[IngestionJobRecord]:
        """적재된 레코드 스냅샷 — 방어 복사본. 외부 ``.clear()`` 등이 내부 영향 없음."""
        return list(self._records)

    def record(self, record: IngestionJobRecord) -> None:
        self._records.append(record)

    def record_many(self, records: list[IngestionJobRecord]) -> None:
        self._records.extend(records)


class MongoIngestionJobsRepository(IngestionJobsRepository):
    """MongoDB ``ingestion_jobs`` 컬렉션 클라이언트 — db-schema §2.3 정합.

    Args:
        client: 사전 구성된 pymongo MongoClient. ``from_settings`` 가 환경 설정에서
            생성하거나, 테스트가 mongomock·mock 객체를 주입한다.
        db_name: 데이터베이스 이름 (Settings.mongo_db 기본값 ``lina_rag``).
        collection_name: 컬렉션 이름. 기본값 ``ingestion_jobs``.

    Raises:
        ImportError: pymongo 미설치 시 ``from_settings`` 호출 단계에서 발생.
    """

    def __init__(
        self,
        client: object,
        db_name: str,
        *,
        collection_name: str = "ingestion_jobs",
    ) -> None:
        # pymongo MongoClient는 dict-style 인덱싱으로 DB·컬렉션을 얻는다 (chunk_lookup 패턴).
        self._collection = client[db_name][collection_name]  # type: ignore[index]

    @classmethod
    def from_settings(
        cls, settings: Settings, *, collection_name: str = "ingestion_jobs"
    ) -> "MongoIngestionJobsRepository":
        """환경 설정에서 MongoClient를 생성해 인스턴스화한다 (운영 경로)."""
        from pymongo import MongoClient

        # mypy: MongoClient는 generic이라 명시 annotation을 요구한다 — 운영 경로에서는
        # Any 타입의 클라이언트를 받아 dict-style 인덱싱만 한다.
        client: MongoClient = MongoClient(settings.mongo_uri)  # type: ignore[type-arg]
        return cls(client=client, db_name=settings.mongo_db, collection_name=collection_name)

    def record(self, record: IngestionJobRecord) -> None:
        self._collection.insert_one(_record_to_doc(record))  # type: ignore[attr-defined]

    def record_many(self, records: list[IngestionJobRecord]) -> None:
        if not records:
            # 빈 입력에서 pymongo insert_many 는 InvalidOperation 을 던지므로 어댑터가
            # short-circuit 해 호출자(그래프 노드)가 별도 분기 없이 호출하도록 한다.
            return
        docs = [_record_to_doc(r) for r in records]
        self._collection.insert_many(docs)  # type: ignore[attr-defined]


def _record_to_doc(record: IngestionJobRecord) -> dict:
    """IngestionJobRecord → Mongo document — enum 은 문자열로 직렬화 (db-schema §2.3).

    ``stage`` / ``status`` 는 StrEnum 이므로 ``.value`` 가 자연스러운 문자열이지만,
    명시적으로 변환해 mongo BSON 인코더에 enum 타입이 전달되지 않도록 한다 (호환성
    안전).
    """
    return {
        "page_id": record.page_id,
        "attachment_id": record.attachment_id,
        "stage": record.stage.value,
        "status": record.status.value,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "error": record.error,
    }
