"""Space Doc-Type Cache — MySQL ``space_doc_type_cache`` 어댑터 [Storage].

--------------------------------------------------
작성자 : 최태성
담당 영역 : ingestion
작성목적 : 문서 분석기 [Agent]가 스페이스 단위로 1회 판별한 doc_type 결과를 캐싱하는
          저장소 어댑터(db-schema §3.1). 같은 스페이스의 후속 페이지는 LLM 재호출 없이
          캐시를 재사용한다(비용·지연 절감). jobs.py 의 ABC + Fake + 실구현 3계층 패턴을
          따른다(`app/CLAUDE.md` §8 — 외부 호출은 어댑터 계층으로 분리).
작성일 : 2026-05-26 (featureI-4b)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-26, 최초 작성, featureI-4b — SpaceDocTypeCache ABC + FakeSpaceDocTypeCache +
    MySQLSpaceDocTypeCache + SpaceDocTypeEntry.
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - sqlalchemy>=2.0 / pymysql (MySQLSpaceDocTypeCache 가 사용)
  - 외부 의존성 0 (base ABC + FakeSpaceDocTypeCache 는 sqlalchemy 미설치 환경에서도 동작)
--------------------------------------------------
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from app.config import Settings
from app.schemas.enums import DocType


@dataclass(slots=True)
class SpaceDocTypeEntry:
    """``space_doc_type_cache`` 단일 레코드 — db-schema §3.1 정합.

    Attributes:
        space_key: Confluence 스페이스 식별자(PK).
        dominant_doc_type: 지배적 문서 유형(폴백 적용 후 실제 사용 값).
        secondary_doc_types: 보조 유형 목록.
        confidence: 판별 신뢰도. < 0.6 이면 호출자가 operation 폴백을 적용한다.
        analyzed_at: 분석 시각.
        sample_count: 분석에 사용한 샘플 페이지 수.
    """

    space_key: str
    dominant_doc_type: DocType
    confidence: float
    analyzed_at: datetime
    sample_count: int
    secondary_doc_types: list[DocType] = field(default_factory=list)


class SpaceDocTypeCache(ABC):
    """``space_doc_type_cache`` 추상 인터페이스 — 문서 분석기가 조회·기록한다."""

    @abstractmethod
    def get(self, space_key: str) -> SpaceDocTypeEntry | None:
        """``space_key`` 의 캐시 엔트리를 반환한다. 없으면 None."""

    @abstractmethod
    def set(self, entry: SpaceDocTypeEntry) -> None:
        """``space_key`` 기준으로 엔트리를 멱등 upsert 한다."""


@dataclass(slots=True)
class FakeSpaceDocTypeCache(SpaceDocTypeCache):
    """In-memory ``SpaceDocTypeCache`` — 테스트·PoC 용(외부 의존성 0)."""

    entries: dict[str, SpaceDocTypeEntry] = field(default_factory=dict)

    def get(self, space_key: str) -> SpaceDocTypeEntry | None:
        return self.entries.get(space_key)

    def set(self, entry: SpaceDocTypeEntry) -> None:
        self.entries[entry.space_key] = entry


class MySQLSpaceDocTypeCache(SpaceDocTypeCache):
    """MySQL ``space_doc_type_cache`` 클라이언트 — db-schema §3.1 정합(SQLAlchemy Core).

    Args:
        engine: 사전 구성된 SQLAlchemy Engine. ``from_settings`` 가 환경 설정에서 생성한다.

    Raises:
        ImportError: sqlalchemy 미설치 시 ``from_settings`` 호출 단계에서 발생.
    """

    _TABLE = "space_doc_type_cache"

    def __init__(self, engine: object) -> None:
        self._engine = engine

    @classmethod
    def from_settings(cls, settings: Settings) -> MySQLSpaceDocTypeCache:
        """환경 설정(`mysql_uri`)에서 Engine 을 생성해 인스턴스화한다(운영 경로)."""
        from sqlalchemy import create_engine

        return cls(engine=create_engine(settings.mysql_uri))

    def get(self, space_key: str) -> SpaceDocTypeEntry | None:
        from sqlalchemy import text

        sql = text(
            "SELECT space_key, dominant_doc_type, secondary_doc_types, confidence, "
            "analyzed_at, sample_count FROM space_doc_type_cache WHERE space_key = :space_key"
        )
        with self._engine.connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(sql, {"space_key": space_key}).mappings().first()
        if row is None:
            return None
        return SpaceDocTypeEntry(
            space_key=row["space_key"],
            dominant_doc_type=DocType(row["dominant_doc_type"]),
            secondary_doc_types=[
                DocType(value) for value in json.loads(row["secondary_doc_types"])
            ],
            confidence=float(row["confidence"]),
            analyzed_at=row["analyzed_at"],
            sample_count=int(row["sample_count"]),
        )

    def set(self, entry: SpaceDocTypeEntry) -> None:
        from sqlalchemy import text

        # MySQL upsert — space_key PK 충돌 시 갱신(멱등). 다른 dialect 는 운영 부트스트랩에서 조정.
        sql = text(
            "INSERT INTO space_doc_type_cache "
            "(space_key, dominant_doc_type, secondary_doc_types, confidence, "
            "analyzed_at, sample_count) "
            "VALUES (:space_key, :dominant, :secondary, :confidence, :analyzed_at, :sample_count) "
            "ON DUPLICATE KEY UPDATE dominant_doc_type=:dominant, secondary_doc_types=:secondary, "
            "confidence=:confidence, analyzed_at=:analyzed_at, sample_count=:sample_count"
        )
        params = {
            "space_key": entry.space_key,
            "dominant": entry.dominant_doc_type.value,
            "secondary": json.dumps([doc_type.value for doc_type in entry.secondary_doc_types]),
            "confidence": entry.confidence,
            "analyzed_at": entry.analyzed_at,
            "sample_count": entry.sample_count,
        }
        with self._engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(sql, params)
