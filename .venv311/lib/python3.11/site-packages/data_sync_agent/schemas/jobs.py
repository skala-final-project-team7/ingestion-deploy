from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Sync Agent sync job schema 정의.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature1 sync job schema 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses 기반
--------------------------------------------------
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from data_sync_agent.schemas._serialization import to_primitive


class SyncJobStatus(StrEnum):
    """Delta sync job lifecycle status."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


@dataclass(slots=True)
class SyncJob:
    """Delta sync job identity and non-sensitive runtime context."""

    sync_id: str
    cloud_id: str
    status: SyncJobStatus
    requested_at: str
    output_dir: str
    previous_snapshot: str

    def __post_init__(self) -> None:
        self.status = SyncJobStatus(self.status)
        self.validate()

    def validate(self) -> None:
        """Sync job 필수값을 검증한다."""
        if not self.sync_id:
            raise ValueError("sync_id is required")
        if not self.cloud_id:
            raise ValueError("cloud_id is required")
        if not self.requested_at:
            raise ValueError("requested_at is required")
        if not self.output_dir:
            raise ValueError("output_dir is required")
        if not self.previous_snapshot:
            raise ValueError("previous_snapshot is required")

    def to_dict(self) -> dict[str, Any]:
        """JSON report나 log-safe context에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)
