from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Failed item과 ingestion report 생성 helper 구현.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature4 helper 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - Data Ingestion Agent canonical schema 기준
--------------------------------------------------
"""

from data_ingestion_agent.schemas import (
    FailedItem,
    FailedItemStage,
    FailedItemType,
    IngestionReport,
    IngestionReportCounts,
    IngestionReportStatus,
)


def build_failed_item(
    *,
    job_id: str,
    stage: FailedItemStage,
    item_type: FailedItemType,
    item_id: str | None,
    error_type: str,
    error_message: str,
    retryable: bool,
    attempt_count: int,
) -> FailedItem:
    """후속 workflow에서 재사용할 failed item schema를 생성한다."""
    return FailedItem(
        job_id=job_id,
        stage=stage,
        item_type=item_type,
        item_id=item_id,
        error_type=error_type,
        error_message=error_message,
        retryable=retryable,
        attempt_count=attempt_count,
    )


def build_ingestion_report(
    *,
    job_id: str,
    spaces_total: int,
    page_refs_total: int,
    pages_succeeded: int,
    documents_written: int,
    failed_items: int,
    skipped_items: int,
    output_paths: dict[str, str] | None = None,
) -> IngestionReport:
    """Pipeline 집계값으로 canonical ingestion report를 생성한다.

    `skipped_items`는 MVP canonical report에 별도 필드가 없으므로 status 계산에만
    반영하고, `page_refs`에는 수집 대상 전체 개수를 보존한다.
    """
    if failed_items > 0:
        status = IngestionReportStatus.COMPLETED_WITH_ERRORS
    elif skipped_items > 0:
        status = IngestionReportStatus.COMPLETED_WITH_ERRORS
    else:
        status = IngestionReportStatus.COMPLETED

    return IngestionReport(
        job_id=job_id,
        status=status,
        counts=IngestionReportCounts(
            spaces=spaces_total,
            page_refs=page_refs_total,
            pages_fetched=pages_succeeded,
            documents_written=documents_written,
            failed_items=failed_items,
        ),
        output_paths=output_paths or {},
    )
