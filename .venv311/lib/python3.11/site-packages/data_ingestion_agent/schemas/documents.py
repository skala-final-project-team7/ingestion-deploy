from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Confluence page 기반 processed document canonical schema 정의.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature1 processed document schema 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses 기반
--------------------------------------------------
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from data_ingestion_agent.schemas._serialization import to_primitive


class AttachmentProcessingStatus(StrEnum):
    """MVP 첨부파일 처리 상태."""

    NOT_SUPPORTED_IN_MVP = "not_supported_in_mvp"


@dataclass(slots=True)
class SpaceInfo:
    """Confluence Space metadata."""

    space_id: str
    space_key: str
    space_name: str

    def __post_init__(self) -> None:
        if not self.space_id:
            raise ValueError("space_id is required")
        if not self.space_key:
            raise ValueError("space_key is required")
        if not self.space_name:
            raise ValueError("space_name is required")


@dataclass(slots=True)
class PageInfo:
    """Confluence Page metadata used by downstream RAG pipeline."""

    page_id: str
    parent_id: str | None
    title: str
    status: str
    depth: int
    child_position: int
    page_url: str
    created_at: str
    last_modified_at: str
    version_number: int

    def __post_init__(self) -> None:
        if not self.page_id:
            raise ValueError("page_id is required")
        if not self.title:
            raise ValueError("title is required")
        if self.status != "current":
            raise ValueError("status must be current")
        if self.depth < 0:
            raise ValueError("depth must be greater than or equal to 0")
        if self.child_position < 0:
            raise ValueError("child_position must be greater than or equal to 0")
        if self.version_number < 0:
            raise ValueError("version_number must be greater than or equal to 0")


@dataclass(slots=True)
class BodyContent:
    """Confluence storage HTML and derived plain text body."""

    storage_html: str
    plain_text: str
    representation: Literal["storage"] = "storage"

    def __post_init__(self) -> None:
        if self.representation != "storage":
            raise ValueError("representation must be storage")


@dataclass(slots=True)
class ProcessedDocumentMetadata:
    """MVP metadata for a processed Confluence page document."""

    content_length: int
    plain_text_length: int
    has_attachments: bool = False
    attachment_processing_status: AttachmentProcessingStatus = (
        AttachmentProcessingStatus.NOT_SUPPORTED_IN_MVP
    )

    def __post_init__(self) -> None:
        if self.content_length < 0:
            raise ValueError("content_length must be greater than or equal to 0")
        if self.plain_text_length < 0:
            raise ValueError("plain_text_length must be greater than or equal to 0")
        if (
            self.attachment_processing_status
            != AttachmentProcessingStatus.NOT_SUPPORTED_IN_MVP
        ):
            raise ValueError("attachment_processing_status must be not_supported_in_mvp")


@dataclass(slots=True)
class ProcessedDocument:
    """Chunking/Embedding/RAG 파이프라인이 소비할 processed document schema."""

    job_id: str
    cloud_id: str
    space: SpaceInfo
    page: PageInfo
    body: BodyContent
    metadata: ProcessedDocumentMetadata
    document_id: str | None = None
    source_type: Literal["confluence_page"] = "confluence_page"

    def __post_init__(self) -> None:
        if self.document_id is None:
            self.document_id = self.build_document_id(
                self.page.page_id,
                self.page.version_number,
            )
        self.validate()

    @staticmethod
    def build_document_id(page_id: str, version_number: int) -> str:
        """page id와 version number로 canonical document id를 생성한다."""
        return f"confluence-page-{page_id}-{version_number}"

    def validate(self) -> None:
        """Processed document canonical constraints를 검증한다."""
        if not self.job_id:
            raise ValueError("job_id is required")
        if not self.cloud_id:
            raise ValueError("cloud_id is required")
        if self.source_type != "confluence_page":
            raise ValueError("source_type must be confluence_page")

        expected_document_id = self.build_document_id(
            self.page.page_id,
            self.page.version_number,
        )
        if self.document_id != expected_document_id:
            raise ValueError(
                f"document_id must be {expected_document_id}, got {self.document_id}"
            )

    def to_dict(self) -> dict[str, Any]:
        """JSON/JSONL 산출물에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)
