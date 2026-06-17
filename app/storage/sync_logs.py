"""Sync Logs Repository — 관리자 대시보드 ``sync_logs`` 적재 어댑터 [Storage]."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import Settings

_TIME_FIELDS = ("startedAt", "completedAt", "createdAt", "updatedAt")


class SyncLogRepository(ABC):
    """``sync_logs`` 적재 추상 인터페이스."""

    @abstractmethod
    def record(self, record: Mapping[str, Any] | object) -> None:
        """동기화 이력 1건을 기록한다."""


@dataclass(slots=True)
class FakeSyncLogRepository(SyncLogRepository):
    """In-memory ``sync_logs`` repository — 테스트·PoC 용."""

    records: list[dict[str, Any]] = field(default_factory=list)

    def record(self, record: Mapping[str, Any] | object) -> None:
        document = _record_to_document(record)
        key = _record_key(document)
        for index, existing in enumerate(self.records):
            if _record_key(existing) == key:
                self.records[index] = document
                return
        self.records.append(document)


class MongoSyncLogRepository(SyncLogRepository):
    """MongoDB ``sync_logs`` 컬렉션 클라이언트."""

    def __init__(
        self,
        client: object,
        db_name: str,
        *,
        collection_name: str = "sync_logs",
    ) -> None:
        self._collection = client[db_name][collection_name]  # type: ignore[index]

    @classmethod
    def from_settings(
        cls, settings: Settings, *, collection_name: str = "sync_logs"
    ) -> MongoSyncLogRepository:
        """환경 설정에서 MongoClient 를 생성해 인스턴스화한다."""
        from pymongo import MongoClient

        client: MongoClient = MongoClient(settings.mongo_uri)  # type: ignore[type-arg]
        return cls(client=client, db_name=settings.mongo_db, collection_name=collection_name)

    def record(self, record: Mapping[str, Any] | object) -> None:
        document = _record_to_document(record)
        self._collection.update_one(  # type: ignore[attr-defined]
            _record_filter(document),
            {"$set": document},
            upsert=True,
        )


def _record_to_document(record: Mapping[str, Any] | object) -> dict[str, Any]:
    """지원 record 형태를 Mongo 저장용 document 로 정규화한다."""
    if isinstance(record, Mapping):
        document = dict(record)
    else:
        to_admin_document = getattr(record, "to_admin_document", None)
        if not callable(to_admin_document):
            raise TypeError("sync log record must be a mapping or expose to_admin_document()")
        document = dict(to_admin_document())

    if not str(document.get("syncId") or document.get("jobId") or "").strip():
        raise ValueError("sync log record requires syncId or jobId")
    if not str(document.get("status") or "").strip():
        raise ValueError("sync log record requires status")

    now = datetime.now(UTC)
    if "createdAt" not in document:
        document["createdAt"] = document.get("startedAt") or document.get("completedAt") or now
    if "updatedAt" not in document:
        document["updatedAt"] = document.get("completedAt") or now

    for field_name in _TIME_FIELDS:
        if field_name in document and document[field_name] is not None:
            document[field_name] = _to_datetime(document[field_name])

    for field_name in ("updatedPages", "deletedPages", "failedPages", "duration"):
        if field_name in document:
            document[field_name] = max(0, int(document[field_name]))

    return document


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    raise ValueError(f"invalid sync log timestamp: {value!r}")


def _record_filter(document: Mapping[str, Any]) -> dict[str, Any]:
    if document.get("jobId"):
        return {"jobId": document["jobId"], "mode": document.get("mode", "")}
    return {"syncId": document.get("syncId", ""), "mode": document.get("mode", "")}


def _record_key(document: Mapping[str, Any]) -> tuple[str, str, str]:
    if document.get("jobId"):
        return ("jobId", str(document["jobId"]), str(document.get("mode", "")))
    return ("syncId", str(document.get("syncId", "")), str(document.get("mode", "")))
