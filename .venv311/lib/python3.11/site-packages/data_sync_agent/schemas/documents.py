from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Sync Agent changed document schema 정의.
          Data Ingestion Agent processed document 계약과 호환되는 필드를 유지한다.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature1 changed document schema 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses 기반
--------------------------------------------------
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from data_sync_agent.schemas._serialization import to_primitive


class ChangeType(StrEnum):
    """Delta sync change classification."""

    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    DELETED_CANDIDATE = "deleted_candidate"
    FAILED = "failed"


class AttachmentProcessingStatus(StrEnum):
    """MVP attachment handling status."""

    NOT_SUPPORTED_IN_MVP = "not_supported_in_mvp"


@dataclass(slots=True)
class ChangedDocument:
    """Chunking/Embedding 단계가 소비할 changed Confluence document schema."""

    sync_id: str
    change_type: ChangeType
    cloud_id: str
    space: dict[str, Any]
    page: dict[str, Any]
    body: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    document_id: str | None = None
    source_type: Literal["confluence_page"] = "confluence_page"

    def __post_init__(self) -> None:
        self.change_type = ChangeType(self.change_type)
        self.metadata.setdefault(
            "attachment_processing_status",
            AttachmentProcessingStatus.NOT_SUPPORTED_IN_MVP,
        )
        if self.document_id is None:
            self.document_id = self.build_document_id(
                str(self.page.get("page_id", "")),
                int(self.page.get("version_number", -1)),
            )
        self.validate()

    @staticmethod
    def build_document_id(page_id: str, version_number: int) -> str:
        """page id와 version number로 canonical document id를 생성한다."""
        return f"confluence-page-{page_id}-{version_number}"

    def validate(self) -> None:
        """Changed document canonical constraints를 검증한다."""
        if not self.sync_id:
            raise ValueError("sync_id is required")
        if self.change_type not in {ChangeType.NEW, ChangeType.UPDATED}:
            raise ValueError("change_type must be new or updated")
        if not self.cloud_id:
            raise ValueError("cloud_id is required")
        if self.source_type != "confluence_page":
            raise ValueError("source_type must be confluence_page")
        if not self.space.get("space_id"):
            raise ValueError("space.space_id is required")
        if not self.page.get("page_id"):
            raise ValueError("page.page_id is required")
        if "version_number" not in self.page:
            raise ValueError("page.version_number is required")
        if self.body.get("representation") != "storage":
            raise ValueError("body.representation must be storage")
        if "storage_html" not in self.body:
            raise ValueError("body.storage_html is required")
        if "plain_text" not in self.body:
            raise ValueError("body.plain_text is required")

        expected_document_id = self.build_document_id(
            str(self.page["page_id"]),
            int(self.page["version_number"]),
        )
        if self.document_id != expected_document_id:
            raise ValueError(
                f"document_id must be {expected_document_id}, got {self.document_id}"
            )

        if (
            self.metadata.get("attachment_processing_status")
            != AttachmentProcessingStatus.NOT_SUPPORTED_IN_MVP
        ):
            raise ValueError("attachment_processing_status must be not_supported_in_mvp")

    def to_dict(self) -> dict[str, Any]:
        """JSON/JSONL changed document 산출물에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)
