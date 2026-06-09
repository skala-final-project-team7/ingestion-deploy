"""Embedding Cache — MongoDB `embedding_cache` 클라이언트 어댑터 [Storage].

--------------------------------------------------
작성자 : 최태성
작성목적 : LINA RAG 파이프라인의 재임베딩 멱등성을 강제하는 ``embedding_cache``
          저장소 어댑터. 동일 ``(chunk_id, version_number)`` 의 재호출에서 임베딩·
          upsert를 스킵하기 위한 경량 키-값 저장소다 (`docs/db-schema.md` §2.4,
          `app/CLAUDE.md` §4). MongoEmbeddingCache는 pymongo 래퍼, FakeEmbeddingCache는
          외부 의존성 없는 in-memory 구현으로 테스트·PoC에 사용한다.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature5-B-3 — EmbeddingCache ABC + MongoEmbeddingCache +
    FakeEmbeddingCache
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - pymongo>=4.7 (project main dependency — MongoEmbeddingCache가 사용)
  - 외부 의존성 0 (base ABC + FakeEmbeddingCache는 pymongo 미설치 환경에서도 동작)
--------------------------------------------------
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from app.config import Settings


@dataclass(frozen=True, slots=True)
class EmbeddingCacheEntry:
    """``embedding_cache`` 단일 레코드 — db-schema §2.4 정합.

    저장 키는 ``chunk_id``. ``dense_hash`` / ``sparse_hash`` 는 벡터 추적용 메타데이터로
    skip 판정에는 사용되지 않는다(skip 판정은 ``version_number`` 동치 비교).
    """

    chunk_id: str
    version_number: int
    dense_hash: str
    sparse_hash: str
    computed_at: datetime


class EmbeddingCache(ABC):
    """Embedding cache 추상 인터페이스 — Ingestion indexer의 멱등성 의존성.

    동일 ``(chunk_id, version_number)`` 로 재호출되면 임베딩·upsert를 스킵하기 위해
    indexer가 본 인터페이스를 통해 캐시 상태를 조회·기록한다. 구체 구현(MongoDB / Fake)
    은 어댑터 패턴으로 갈아끼울 수 있다 (`app/CLAUDE.md` §8).
    """

    @abstractmethod
    def get_cached_version(self, chunk_id: str) -> int | None:
        """``chunk_id`` 의 캐시 버전을 반환한다. 캐시에 없으면 ``None``.

        Indexer는 반환값과 현재 ``version_number`` 를 비교해 skip 여부를 결정한다.
        """

    @abstractmethod
    def set_cached_version(
        self,
        chunk_id: str,
        version_number: int,
        *,
        dense_hash: str,
        sparse_hash: str,
        computed_at: datetime,
    ) -> None:
        """``chunk_id`` 의 캐시를 ``(version_number, hashes, computed_at)`` 으로 upsert한다.

        같은 ``chunk_id`` 가 이미 있으면 덮어쓴다 (멱등 upsert).
        """


class MongoEmbeddingCache(EmbeddingCache):
    """MongoDB ``embedding_cache`` 컬렉션 클라이언트 — db-schema §2.4 정합.

    Args:
        client: 사전 구성된 pymongo MongoClient. ``from_settings`` 가 환경 설정에서
            생성하거나, 테스트가 mongomock·mock 객체를 주입한다.
        db_name: 데이터베이스 이름 (Settings.mongo_db 기본값 ``lina_rag``).
        collection_name: 컬렉션 이름. 기본값 ``embedding_cache``.

    Raises:
        ImportError: pymongo 미설치 시 ``from_settings`` 호출 단계에서 발생.
    """

    def __init__(
        self,
        client: object,
        db_name: str,
        *,
        collection_name: str = "embedding_cache",
    ) -> None:
        # pymongo MongoClient는 dict-style 인덱싱으로 DB·컬렉션을 얻는다.
        self._collection = client[db_name][collection_name]  # type: ignore[index]

    @classmethod
    def from_settings(
        cls, settings: Settings, *, collection_name: str = "embedding_cache"
    ) -> "MongoEmbeddingCache":
        """환경 설정에서 MongoClient를 생성해 인스턴스화한다 (운영 경로)."""
        from pymongo import MongoClient

        # mypy: MongoClient는 generic이라 명시 annotation을 요구한다 — 운영 경로에서는
        # Any 타입의 클라이언트를 받아 MongoEmbeddingCache 측이 dict-style 인덱싱만 한다.
        client: MongoClient = MongoClient(settings.mongo_uri)  # type: ignore[type-arg]
        return cls(client=client, db_name=settings.mongo_db, collection_name=collection_name)

    def get_cached_version(self, chunk_id: str) -> int | None:
        # 인덱스: db-schema §2.4 — chunk_id 단일 키 조회. 운영에서는 chunk_id 유니크
        # 인덱스로 O(1) 룩업. find_one은 매칭 없으면 None을 반환한다.
        doc = self._collection.find_one(
            {"chunk_id": chunk_id},
            projection={"version_number": 1, "_id": 0},
        )
        if doc is None:
            return None
        version = doc.get("version_number")
        return int(version) if version is not None else None

    def set_cached_version(
        self,
        chunk_id: str,
        version_number: int,
        *,
        dense_hash: str,
        sparse_hash: str,
        computed_at: datetime,
    ) -> None:
        # 멱등 upsert: 같은 chunk_id가 있으면 덮어쓰고 없으면 새로 만든다.
        self._collection.update_one(
            {"chunk_id": chunk_id},
            {
                "$set": {
                    "version_number": version_number,
                    "dense_hash": dense_hash,
                    "sparse_hash": sparse_hash,
                    "computed_at": computed_at,
                }
            },
            upsert=True,
        )


@dataclass(slots=True)
class FakeEmbeddingCache(EmbeddingCache):
    """In-memory ``EmbeddingCache`` — 테스트·PoC용 (외부 의존성 0).

    같은 프로세스 안에서 결정론 동작. ``entries`` 속성은 디버깅·assert 편의를 위해
    노출된다 (테스트에서 cache 상태를 직접 확인할 때 사용).
    """

    entries: dict[str, EmbeddingCacheEntry] = field(default_factory=dict)

    def get_cached_version(self, chunk_id: str) -> int | None:
        entry = self.entries.get(chunk_id)
        return entry.version_number if entry else None

    def set_cached_version(
        self,
        chunk_id: str,
        version_number: int,
        *,
        dense_hash: str,
        sparse_hash: str,
        computed_at: datetime,
    ) -> None:
        self.entries[chunk_id] = EmbeddingCacheEntry(
            chunk_id=chunk_id,
            version_number=version_number,
            dense_hash=dense_hash,
            sparse_hash=sparse_hash,
            computed_at=computed_at,
        )
