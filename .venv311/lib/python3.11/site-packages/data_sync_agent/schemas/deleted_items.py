from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Sync Agent deleted candidate schema 정의.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature1 deleted item schema 구현
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
from data_sync_agent.schemas.snapshots import build_page_key


class DeleteType(StrEnum):
    """MVP delete classification."""

    DELETED_CANDIDATE = "deleted_candidate"


@dataclass(slots=True)
class DeletedItem:
    """Snapshot에서 사라진 Page를 확정 삭제가 아닌 삭제 후보로 기록하는 schema."""

    sync_id: str
    cloud_id: str
    space_id: str
    page_id: str
    title: str
    page_url: str = ""
    last_seen_version: int | None = None
    detected_at: str = ""
    deletion_status: Literal["candidate"] = "candidate"
    page_key: str | None = None
    delete_type: DeleteType = DeleteType.DELETED_CANDIDATE
    detection_method: Literal["snapshot_missing"] = "snapshot_missing"
    requires_confirmation: bool = True

    def __post_init__(self) -> None:
        self.delete_type = DeleteType(self.delete_type)
        if self.page_key is None:
            self.page_key = build_page_key(self.cloud_id, self.space_id, self.page_id)
        self.validate()

    def validate(self) -> None:
        """Deleted candidate 필수값과 안전 상태를 검증한다."""
        if not self.sync_id:
            raise ValueError("sync_id is required")
        if self.page_key != build_page_key(self.cloud_id, self.space_id, self.page_id):
            raise ValueError("page_key must match cloud_id:space_id:page_id")
        if not self.title:
            raise ValueError("title is required")
        if self.last_seen_version is not None and self.last_seen_version < 0:
            raise ValueError("last_seen_version must be greater than or equal to 0")
        if self.deletion_status != "candidate":
            raise ValueError("deletion_status must be candidate")
        if self.delete_type != DeleteType.DELETED_CANDIDATE:
            raise ValueError("delete_type must be deleted_candidate")
        if self.detection_method != "snapshot_missing":
            raise ValueError("detection_method must be snapshot_missing")
        if self.requires_confirmation is not True:
            raise ValueError("requires_confirmation must be true")

    def to_dict(self) -> dict[str, Any]:
        """JSON/JSONL deleted item 산출물에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)
