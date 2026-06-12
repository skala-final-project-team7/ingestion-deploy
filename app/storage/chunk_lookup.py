"""Chunk Text Lookup — chunk_id → 풀 텍스트 + 첨부 download_url 어댑터 [Storage].

--------------------------------------------------
작성자 : 최태성
담당 영역 : ingestion
작성목적 : 청크 단위 풀 텍스트와 첨부 다운로드 URL을 ``chunk_id`` 키로 조회하는 어댑터.
          payload의 ``text_preview`` (첫 200자) 한계를 보완하고, 첨부 청크의 사용자
          노출용 다운로드 URL을 ``Source.download_url`` 에 채우기 위한 인프라다
          (`docs/db-schema.md` §2.5, `app/CLAUDE.md` §8 — 외부 호출은 어댑터/클라이언트
          계층으로 분리).
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, 풀 텍스트 lookup 인프라 — ChunkTextLookup ABC +
    ChunkLookupRecord 값 객체 + FakeChunkTextLookup (in-memory) + MongoChunkTextLookup.
    적재 흐름(인덱싱 시 chunk_lookup upsert)은 별도 후속 milestone.
  - 2026-05-18, 풀 텍스트 lookup Phase 2 — ABC에 upsert / upsert_many 추상 메서드 추가
    + Fake (dict 갱신) / Mongo (replace_one + bulk_write/ReplaceOne) 구현. updated_at은
    어댑터가 적재 시점에 자동 부여한다 (db-schema §2.5 정합). 빈 입력 short-circuit으로
    pymongo InvalidOperation 회피.
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - pymongo>=4.7 (project main dependency — MongoChunkTextLookup이 사용)
  - 외부 의존성 0 (base ABC + FakeChunkTextLookup은 pymongo 미설치 환경에서도 동작)
--------------------------------------------------
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings


@dataclass(frozen=True, slots=True)
class ChunkLookupRecord:
    """``chunk_lookup`` 단일 레코드 — db-schema §2.5 정합.

    Attributes:
        chunk_id: 결정론적 청크 식별자 (40자 SHA1 hex).
        text: 청크 풀 텍스트. 답변 생성기·검증기가 ``text_preview`` 200자 한계를
            벗어나는 컨텍스트를 필요로 할 때 본 필드를 조회한다.
        download_url: 첨부 청크일 때만 채워지는 사용자 노출용 다운로드 URL.
            본문 청크는 None.
    """

    chunk_id: str
    text: str
    download_url: str | None


class ChunkTextLookup(ABC):
    """Chunk text/download_url 추상 인터페이스 — 풀 텍스트 lookup 어댑터의 경계.

    답변 생성기·검증기는 풀 텍스트가 필요할 때 본 인터페이스로 조회한다. 출처 카드
    구성(``_chunk_to_source``)은 첨부 청크의 ``download_url`` 만 조회해 채운다.
    구체 구현(MongoDB / Fake)은 어댑터 패턴으로 갈아끼울 수 있다 (`app/CLAUDE.md` §8).
    """

    @abstractmethod
    def fetch(self, chunk_id: str) -> ChunkLookupRecord | None:
        """``chunk_id`` 의 레코드 1건을 조회한다.

        Args:
            chunk_id: 결정론적 청크 식별자.

        Returns:
            매칭 레코드 또는 ``None`` (컬렉션에 없음).
        """

    @abstractmethod
    def fetch_many(self, chunk_ids: list[str]) -> dict[str, ChunkLookupRecord]:
        """다수 ``chunk_id`` 를 배치 조회한다.

        Args:
            chunk_ids: 조회할 청크 ID 목록.

        Returns:
            ``chunk_id`` → 레코드 dict. 미존재 chunk_id는 결과 dict에 포함되지 않는다.
        """

    @abstractmethod
    def upsert(self, record: ChunkLookupRecord) -> None:
        """단일 레코드를 ``chunk_id`` 키로 upsert한다.

        Args:
            record: 적재할 레코드. 동일 ``chunk_id`` 가 있으면 본 레코드로 덮어쓴다
                (운영은 ``replace_one(upsert=True)`` 시맨틱). ``updated_at`` 은 어댑터
                가 적재 시점(UTC)에 자동 부여한다.
        """

    @abstractmethod
    def upsert_many(self, records: list[ChunkLookupRecord]) -> None:
        """다수 레코드를 배치 upsert한다.

        Args:
            records: 적재할 레코드 목록. 빈 입력은 noop으로 처리한다 (운영은 pymongo
                ``bulk_write`` 가 빈 ops에서 ``InvalidOperation`` 을 발생시킴 — 호출
                자에게 책임 떠넘기지 않도록 어댑터가 short-circuit).
        """


class FakeChunkTextLookup(ChunkTextLookup):
    """In-memory ChunkTextLookup — 테스트·PoC용 (외부 의존성 0).

    초기화 시 dict로 레코드를 주입하고, 이후 ``fetch`` / ``fetch_many`` 가 dict 조회로
    동작한다. 결정론적이며 pymongo 미설치 환경에서도 동작한다.

    Args:
        records: 초기 레코드 dict. ``chunk_id`` 키. None이면 빈 dict로 시작한다.
    """

    def __init__(self, records: dict[str, ChunkLookupRecord] | None = None) -> None:
        self._records: dict[str, ChunkLookupRecord] = dict(records or {})

    def add(self, record: ChunkLookupRecord) -> None:
        """레코드 1건을 추가한다. 같은 ``chunk_id`` 가 있으면 덮어쓴다."""
        self._records[record.chunk_id] = record

    def fetch(self, chunk_id: str) -> ChunkLookupRecord | None:
        return self._records.get(chunk_id)

    def fetch_many(self, chunk_ids: list[str]) -> dict[str, ChunkLookupRecord]:
        return {cid: self._records[cid] for cid in chunk_ids if cid in self._records}

    def upsert(self, record: ChunkLookupRecord) -> None:
        # in-memory 구현은 ``add`` 와 동일 시맨틱 (dict 덮어쓰기). ABC 계약 정합을 위해
        # 별도 메서드로 노출한다.
        self._records[record.chunk_id] = record

    def upsert_many(self, records: list[ChunkLookupRecord]) -> None:
        for record in records:
            self._records[record.chunk_id] = record


class MongoChunkTextLookup(ChunkTextLookup):
    """MongoDB ``chunk_lookup`` 컬렉션 클라이언트 — db-schema §2.5 정합.

    Args:
        client: 사전 구성된 pymongo MongoClient. ``from_settings`` 가 환경 설정에서
            생성하거나, 테스트가 mongomock·mock 객체를 주입한다.
        db_name: 데이터베이스 이름 (Settings.mongo_db 기본값 ``lina_rag``).
        collection_name: 컬렉션 이름. 기본값 ``chunk_lookup``.

    Raises:
        ImportError: pymongo 미설치 시 ``from_settings`` 호출 단계에서 발생.
    """

    def __init__(
        self,
        client: object,
        db_name: str,
        *,
        collection_name: str = "chunk_lookup",
    ) -> None:
        # pymongo MongoClient는 dict-style 인덱싱으로 DB·컬렉션을 얻는다.
        self._collection = client[db_name][collection_name]  # type: ignore[index]

    @classmethod
    def from_settings(
        cls, settings: Settings, *, collection_name: str = "chunk_lookup"
    ) -> "MongoChunkTextLookup":
        """환경 설정에서 MongoClient를 생성해 인스턴스화한다 (운영 경로)."""
        from pymongo import MongoClient

        # mypy: MongoClient는 generic이라 명시 annotation을 요구한다 — 운영 경로에서는
        # Any 타입의 클라이언트를 받아 dict-style 인덱싱만 한다.
        client: MongoClient = MongoClient(settings.mongo_uri)  # type: ignore[type-arg]
        return cls(client=client, db_name=settings.mongo_db, collection_name=collection_name)

    def fetch(self, chunk_id: str) -> ChunkLookupRecord | None:
        # 인덱스: db-schema §2.5 — chunk_id 단일 키 unique 인덱스로 O(1) 룩업.
        doc = self._collection.find_one(
            {"chunk_id": chunk_id},
            projection={"chunk_id": 1, "text": 1, "download_url": 1, "_id": 0},
        )
        if doc is None:
            return None
        return _doc_to_record(doc)

    def fetch_many(self, chunk_ids: list[str]) -> dict[str, ChunkLookupRecord]:
        if not chunk_ids:
            return {}
        cursor = self._collection.find(
            {"chunk_id": {"$in": list(chunk_ids)}},
            projection={"chunk_id": 1, "text": 1, "download_url": 1, "_id": 0},
        )
        return {doc["chunk_id"]: _doc_to_record(doc) for doc in cursor}

    def upsert(self, record: ChunkLookupRecord) -> None:
        # 단건 upsert는 replace_one(upsert=True)로 처리 — chunk_id unique 인덱스(db-schema
        # §2.5)와 정합. updated_at은 적재 시점에 어댑터가 부여해 호출자 책임 분리.
        self._collection.replace_one(  # type: ignore[attr-defined]
            {"chunk_id": record.chunk_id},
            _record_to_doc(record),
            upsert=True,
        )

    def upsert_many(self, records: list[ChunkLookupRecord]) -> None:
        if not records:
            # 빈 입력에서 pymongo bulk_write 는 InvalidOperation 을 던지므로 어댑터가
            # short-circuit 해 호출자(indexer)가 별도 분기 없이 호출하도록 한다.
            return
        # ReplaceOne 은 호출 시점에 lazy import — pymongo 가 미설치된 테스트 환경에서도
        # 모듈 import 가 깨지지 않게 한다 (Fake 경로는 본 모듈 import만으로 동작).
        from pymongo import ReplaceOne

        ops = [
            ReplaceOne(
                {"chunk_id": record.chunk_id},
                _record_to_doc(record),
                upsert=True,
            )
            for record in records
        ]
        self._collection.bulk_write(ops)  # type: ignore[attr-defined]


def _doc_to_record(doc: dict) -> ChunkLookupRecord:
    """Mongo document → ChunkLookupRecord 안전 변환.

    ``download_url`` 은 본문 청크에서 null이거나 누락될 수 있으므로 ``get`` 으로 안전
    접근한다. ``text`` 가 누락된 비정상 문서는 빈 문자열로 떨어뜨려 호출자가 None이
    아닌 정상 record를 받도록 한다(스키마 보존).
    """
    return ChunkLookupRecord(
        chunk_id=str(doc["chunk_id"]),
        text=str(doc.get("text") or ""),
        download_url=_optional_str(doc.get("download_url")),
    )


def _record_to_doc(record: ChunkLookupRecord) -> dict:
    """ChunkLookupRecord → Mongo document — ``updated_at`` 을 적재 시점(UTC)에 부여.

    db-schema §2.5 의 4개 필드(chunk_id / text / download_url / updated_at)를 모두
    채운다. 본문 청크의 ``download_url`` 은 None 그대로 저장해 fetch 시 None으로 복원
    된다.
    """
    return {
        "chunk_id": record.chunk_id,
        "text": record.text,
        "download_url": record.download_url,
        "updated_at": datetime.now(UTC),
    }


def _optional_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)
