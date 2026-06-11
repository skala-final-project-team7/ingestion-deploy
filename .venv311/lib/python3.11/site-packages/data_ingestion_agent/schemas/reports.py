from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Ingestion Agent ingestion report schema 정의.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature1 report schema 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses 기반
--------------------------------------------------
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from data_ingestion_agent.schemas._serialization import to_primitive


class IngestionReportStatus(StrEnum):
    """Full crawl job 결과 상태."""

    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


@dataclass(slots=True)
class IngestionReportCounts:
    """Ingestion report count summary."""

    spaces: int = 0
    page_refs: int = 0
    pages_fetched: int = 0
    documents_written: int = 0
    failed_items: int = 0

    def __post_init__(self) -> None:
        for field_name in (
            "spaces",
            "page_refs",
            "pages_fetched",
            "documents_written",
            "failed_items",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be greater than or equal to 0")


@dataclass(slots=True)
class IngestionReport:
    """Ingestion job의 최종 상태, count, output path를 기록하는 schema."""

    job_id: str
    status: IngestionReportStatus
    counts: IngestionReportCounts
    output_paths: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Report 필수값과 output path key/value를 검증한다."""
        if not self.job_id:
            raise ValueError("job_id is required")
        for output_name, output_path in self.output_paths.items():
            if not output_name:
                raise ValueError("output path name is required")
            if not output_path:
                raise ValueError("output path is required")

    def to_dict(self) -> dict[str, Any]:
        """JSON report 산출물에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)
