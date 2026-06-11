from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Sync Agent changed page processing service 구현.
          diff 결과의 new/updated Page만 상세 조회해 changed document로 변환한다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature5 changed page processor 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - Protocol 기반 fake client 주입 가능
--------------------------------------------------
"""

from dataclasses import dataclass
from typing import Any, Protocol

from data_sync_agent.confluence import ConfluenceApiError
from data_sync_agent.extraction import HtmlExtractionResult, extract_storage_html
from data_sync_agent.schemas import (
    ChangedDocument,
    ChangeType,
    FailedItem,
    FailedItemStage,
    FailedItemType,
)
from data_sync_agent.sync.diff_engine import PageChange


class PageDetailClient(Protocol):
    """Page 상세 조회에 필요한 client interface."""

    def get_page_detail(self, page_id: str) -> dict[str, Any]:
        """Confluence Page detail을 반환한다."""


@dataclass(frozen=True, slots=True)
class ChangedPageProcessingResult:
    """Changed page processing 결과."""

    changed_documents: list[ChangedDocument]
    failed_items: list[FailedItem]


class ChangedPageProcessor:
    """Diff 결과 중 new/updated Page를 changed document로 변환한다."""

    def __init__(self, *, client: PageDetailClient) -> None:
        self.client = client

    def process(
        self,
        page_changes: list[PageChange],
        *,
        sync_id: str,
        cloud_id: str,
        detected_at: str,
    ) -> ChangedPageProcessingResult:
        """new/updated Page만 상세 조회하고 partial failure를 failed item으로 기록한다."""
        changed_documents: list[ChangedDocument] = []
        failed_items: list[FailedItem] = []

        for page_change in page_changes:
            if page_change.change_type not in {ChangeType.NEW, ChangeType.UPDATED}:
                continue
            if page_change.current is None:
                continue

            page = page_change.current
            try:
                page_detail = self.client.get_page_detail(page.page_id)
                changed_documents.append(
                    build_changed_document(
                        page_change=page_change,
                        page_detail=page_detail,
                        sync_id=sync_id,
                        cloud_id=cloud_id,
                        detected_at=detected_at,
                    )
                )
            except Exception as exc:
                failed_items.append(
                    _failed_item_from_exception(
                        exc,
                        sync_id=sync_id,
                        page_id=page.page_id,
                    )
                )

        return ChangedPageProcessingResult(
            changed_documents=changed_documents,
            failed_items=failed_items,
        )


def build_changed_document(
    *,
    page_change: PageChange,
    page_detail: dict[str, Any],
    sync_id: str,
    cloud_id: str,
    detected_at: str,
) -> ChangedDocument:
    """PageChange와 Page detail response를 ChangedDocument schema로 변환한다."""
    if page_change.current is None:
        raise ValueError("page_change.current is required")
    page = page_change.current
    extraction = extract_storage_html(_storage_html_from_detail(page_detail))
    version_number = _version_number(page_detail, fallback=page.version_number)
    page_id = str(page_detail.get("id") or page.page_id)

    return ChangedDocument(
        sync_id=sync_id,
        change_type=page_change.change_type,
        cloud_id=cloud_id,
        space={
            "space_id": page.space_id,
            "space_key": page.space_key,
            "space_name": page.space_name,
        },
        page={
            "page_key": page.page_key,
            "space_id": page.space_id,
            "page_id": page_id,
            "title": str(page_detail.get("title") or page.title),
            "status": str(page_detail.get("status") or page.status),
            "page_url": _page_url(page_detail, fallback=page.page_url),
            "last_modified_at": _last_modified_at(
                page_detail,
                fallback=page.last_modified_at,
            ),
            "version_number": version_number,
        },
        body={
            "representation": "storage",
            "storage_html": extraction.storage_html,
            "plain_text": extraction.plain_text,
        },
        metadata=_metadata(extraction, detected_at=detected_at),
    )


def _storage_html_from_detail(page_detail: dict[str, Any]) -> str:
    body = page_detail.get("body")
    if not isinstance(body, dict):
        return ""
    storage = body.get("storage")
    if not isinstance(storage, dict):
        return ""
    value = storage.get("value")
    return value if isinstance(value, str) else ""


def _version_number(page_detail: dict[str, Any], *, fallback: int) -> int:
    version = page_detail.get("version")
    if isinstance(version, dict) and "number" in version:
        return int(version["number"])
    return fallback


def _last_modified_at(page_detail: dict[str, Any], *, fallback: str) -> str:
    version = page_detail.get("version")
    if isinstance(version, dict):
        created_at = version.get("createdAt") or version.get("created_at")
        if isinstance(created_at, str) and created_at:
            return created_at
    value = page_detail.get("lastModifiedAt")
    if isinstance(value, str) and value:
        return value
    return fallback


def _page_url(page_detail: dict[str, Any], *, fallback: str) -> str:
    links = page_detail.get("_links")
    if isinstance(links, dict):
        webui = links.get("webui")
        if isinstance(webui, str) and webui:
            return webui
    value = page_detail.get("page_url") or page_detail.get("url")
    return str(value or fallback)


def _metadata(
    extraction: HtmlExtractionResult,
    *,
    detected_at: str,
) -> dict[str, Any]:
    return {
        "detected_at": detected_at,
        "content_length": len(extraction.storage_html),
        "plain_text_length": len(extraction.plain_text),
        "has_attachments": False,
        "has_unsupported_content": extraction.has_unsupported_content,
        "attachment_processing_status": "not_supported_in_mvp",
    }


def _failed_item_from_exception(
    exc: Exception,
    *,
    sync_id: str,
    page_id: str,
) -> FailedItem:
    retryable = False
    attempt_count = 1
    error_type = type(exc).__name__
    if isinstance(exc, ConfluenceApiError):
        retryable = exc.retryable
        attempt_count = exc.attempt_count
        error_type = exc.error_type
    return FailedItem(
        sync_id=sync_id,
        stage=FailedItemStage.FETCH_PAGE_DETAIL,
        item_type=FailedItemType.PAGE,
        item_id=page_id,
        error_type=error_type,
        error_message=_safe_error_message(str(exc)),
        retryable=retryable,
        attempt_count=attempt_count,
    )


def _safe_error_message(message: str) -> str:
    return (
        message.replace("Authorization", "<redacted-header>")
        .replace("Bearer", "<redacted-auth-scheme>")
        .replace("access_token", "<redacted-token-field>")
    )


__all__ = [
    "ChangedPageProcessingResult",
    "ChangedPageProcessor",
    "PageDetailClient",
    "build_changed_document",
    "extract_storage_html",
]
