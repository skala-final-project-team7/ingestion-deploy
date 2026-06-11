from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Confluence Page 상세 응답을 canonical processed document로 변환.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature4 page detail mapper 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - Data Ingestion Agent feature1-3 schema/extractor 기준
--------------------------------------------------
"""

from typing import Any

from data_ingestion_agent.extraction import extract_storage_html
from data_ingestion_agent.schemas import (
    BodyContent,
    PageInfo,
    ProcessedDocument,
    ProcessedDocumentMetadata,
    SpaceInfo,
)


class PageDetailMapper:
    """Confluence page detail dict를 ProcessedDocument로 변환한다."""

    def to_processed_document(
        self,
        *,
        job_id: str,
        cloud_id: str,
        space: SpaceInfo,
        page_ref: dict[str, Any],
        page_detail: dict[str, Any],
    ) -> ProcessedDocument:
        """Page 상세 응답과 page tree metadata를 canonical schema로 매핑한다."""
        page_id = str(page_detail.get("id") or page_ref.get("id") or "")
        storage_html = _extract_storage_html(page_detail)
        extraction_result = extract_storage_html(storage_html)
        version = _dict_value(page_detail, "version")
        version_number = int(version.get("number") or 0)
        page_url = _extract_page_url(page_detail)

        return ProcessedDocument(
            job_id=job_id,
            cloud_id=cloud_id,
            space=space,
            page=PageInfo(
                page_id=page_id,
                parent_id=_optional_str(
                    page_detail.get("parentId") or page_ref.get("parentId")
                ),
                title=str(page_detail.get("title") or ""),
                status=str(page_detail.get("status") or "current"),
                depth=int(page_ref.get("depth") or 0),
                child_position=int(
                    page_ref.get("position")
                    if page_ref.get("position") is not None
                    else page_ref.get("child_position") or 0
                ),
                page_url=page_url,
                created_at=str(page_detail.get("createdAt") or ""),
                last_modified_at=str(
                    version.get("createdAt")
                    or page_detail.get("lastModifiedAt")
                    or page_detail.get("createdAt")
                    or ""
                ),
                version_number=version_number,
            ),
            body=BodyContent(
                storage_html=extraction_result.storage_html,
                plain_text=extraction_result.plain_text,
            ),
            metadata=ProcessedDocumentMetadata(
                content_length=len(extraction_result.storage_html),
                plain_text_length=len(extraction_result.plain_text),
                has_attachments=False,
            ),
        )


def _extract_storage_html(page_detail: dict[str, Any]) -> str:
    body = _dict_value(page_detail, "body")
    storage = _dict_value(body, "storage")
    value = storage.get("value")
    return value if isinstance(value, str) else ""


def _extract_page_url(page_detail: dict[str, Any]) -> str:
    links = _dict_value(page_detail, "_links")
    webui = links.get("webui")
    if isinstance(webui, str):
        return webui
    return ""


def _dict_value(source: dict[str, Any], key: str) -> dict[str, Any]:
    value = source.get(key)
    return value if isinstance(value, dict) else {}


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
