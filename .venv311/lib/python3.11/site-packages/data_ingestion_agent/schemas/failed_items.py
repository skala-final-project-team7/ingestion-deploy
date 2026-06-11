from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Ingestion Agent failed item schema 정의.
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

from data_ingestion_agent.schemas._serialization import to_primitive


class FailedItemStage(StrEnum):
    """Failed item이 발생한 ingestion stage."""

    LIST_SPACES = "list_spaces"
    COLLECT_PAGE_TREE = "collect_page_tree"
    FETCH_PAGE_DETAIL = "fetch_page_detail"
    TRANSFORM_HTML = "transform_html"
    WRITE_OUTPUT = "write_output"


class FailedItemType(StrEnum):
    """Failed item domain type."""

    SPACE = "space"
    PAGE = "page"
    DOCUMENT = "document"


@dataclass(slots=True)
class FailedItem:
    """Page 단위 실패 또는 job stage 실패를 기록하는 schema."""

    job_id: str
    stage: FailedItemStage
    item_type: FailedItemType
    item_id: str | None
    error_type: str
    error_message: str
    retryable: bool
    attempt_count: int
    status: Literal["failed"] = "failed"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Failed item 필수값과 attempt count를 검증한다."""
        if not self.job_id:
            raise ValueError("job_id is required")
        if self.status != "failed":
            raise ValueError("status must be failed")
        if not self.error_type:
            raise ValueError("error_type is required")
        if not self.error_message:
            raise ValueError("error_message is required")
        if self.attempt_count < 1:
            raise ValueError("attempt_count must be greater than or equal to 1")

    def to_dict(self) -> dict[str, Any]:
        """JSON/JSONL 산출물에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)
