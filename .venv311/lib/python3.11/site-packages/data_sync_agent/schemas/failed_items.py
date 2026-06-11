from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Sync Agent failed item schema 정의.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature1 failed item schema 구현
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


class FailedItemStage(StrEnum):
    """Failed item이 발생한 sync stage."""

    LOAD_PREVIOUS_SNAPSHOT = "load_previous_snapshot"
    LIST_SPACES = "list_spaces"
    FETCH_PAGE_METADATA = "fetch_page_metadata"
    DIFF_SNAPSHOTS = "diff_snapshots"
    FETCH_PAGE_DETAIL = "fetch_page_detail"
    TRANSFORM_CHANGED_HTML = "transform_changed_html"
    WRITE_OUTPUT = "write_output"


class FailedItemType(StrEnum):
    """Failed item domain type."""

    SYNC_JOB = "sync_job"
    SPACE = "space"
    PAGE = "page"
    SNAPSHOT = "snapshot"
    DOCUMENT = "document"
    MESSAGE = "message"


@dataclass(slots=True)
class FailedItem:
    """Page 단위 실패 또는 sync stage 실패를 기록하는 schema."""

    sync_id: str
    stage: FailedItemStage
    item_type: FailedItemType
    item_id: str | None
    error_type: str
    error_message: str
    retryable: bool
    attempt_count: int
    status: Literal["failed"] = "failed"

    def __post_init__(self) -> None:
        self.stage = FailedItemStage(self.stage)
        self.item_type = FailedItemType(self.item_type)
        self.validate()

    def validate(self) -> None:
        """Failed item 필수값과 attempt count를 검증한다."""
        if not self.sync_id:
            raise ValueError("sync_id is required")
        if self.status != "failed":
            raise ValueError("status must be failed")
        if not self.error_type:
            raise ValueError("error_type is required")
        if not self.error_message:
            raise ValueError("error_message is required")
        if self.attempt_count < 1:
            raise ValueError("attempt_count must be greater than or equal to 1")

    def to_dict(self) -> dict[str, Any]:
        """JSON/JSONL failed item 산출물에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)
