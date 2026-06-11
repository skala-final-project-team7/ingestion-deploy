from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Sync Agent downstream message payload schema 정의.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature1 message payload schema 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses 기반
--------------------------------------------------
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from data_sync_agent.schemas._serialization import to_primitive
from data_sync_agent.schemas.documents import ChangeType


class MessageEventType(StrEnum):
    """후속 처리 단계가 소비할 local message event type."""

    CHUNKING_REQUESTED = "chunking_requested"
    DELETE_CANDIDATE_DETECTED = "delete_candidate_detected"


@dataclass(slots=True)
class MessagePayload:
    """RabbitMQ 후속 연동을 고려한 local message payload schema."""

    sync_id: str
    event_type: MessageEventType
    page_id: str
    space_id: str
    document_id: str | None
    change_type: ChangeType
    payload_ref: str
    payload_id: str | None = None
    idempotency_key: str | None = None
    operation: str | None = None
    downstream_target: str = "chunking_embedding_pipeline"
    source_type: Literal["confluence_page"] = "confluence_page"

    def __post_init__(self) -> None:
        self.event_type = MessageEventType(self.event_type)
        self.change_type = ChangeType(self.change_type)
        if self.operation is None:
            self.operation = _operation_for_event_type(self.event_type)
        if self.payload_id is None:
            payload_suffix = self.document_id or str(self.change_type)
            self.payload_id = (
                f"{self.sync_id}:{self.event_type}:{self.space_id}:"
                f"{self.page_id}:{payload_suffix}"
            )
        if self.idempotency_key is None:
            self.idempotency_key = self.payload_id
        self.validate()

    def validate(self) -> None:
        """Message payload event와 대상 참조를 검증한다."""
        if not self.sync_id:
            raise ValueError("sync_id is required")
        if self.source_type != "confluence_page":
            raise ValueError("source_type must be confluence_page")
        if not self.page_id:
            raise ValueError("page_id is required")
        if not self.space_id:
            raise ValueError("space_id is required")
        if not self.payload_ref:
            raise ValueError("payload_ref is required")
        if not self.payload_id:
            raise ValueError("payload_id is required")
        if not self.idempotency_key:
            raise ValueError("idempotency_key is required")
        if not self.operation:
            raise ValueError("operation is required")
        if not self.downstream_target:
            raise ValueError("downstream_target is required")
        if self.event_type == MessageEventType.CHUNKING_REQUESTED:
            if self.change_type not in {ChangeType.NEW, ChangeType.UPDATED}:
                raise ValueError("chunking_requested requires new or updated change_type")
            if not self.document_id:
                raise ValueError("chunking_requested requires document_id")
            if self.operation != "page_changed":
                raise ValueError("chunking_requested operation must be page_changed")
        if self.event_type == MessageEventType.DELETE_CANDIDATE_DETECTED:
            if self.change_type != ChangeType.DELETED_CANDIDATE:
                raise ValueError(
                    "delete_candidate_detected requires deleted_candidate change_type"
                )
            if self.document_id is not None:
                raise ValueError("delete_candidate_detected document_id must be None")
            if self.operation != "page_deleted_candidate":
                raise ValueError(
                    "delete_candidate_detected operation must be page_deleted_candidate"
                )

    def to_dict(self) -> dict[str, Any]:
        """JSON/JSONL message 산출물에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)


def _operation_for_event_type(event_type: MessageEventType) -> str:
    if event_type == MessageEventType.CHUNKING_REQUESTED:
        return "page_changed"
    return "page_deleted_candidate"
