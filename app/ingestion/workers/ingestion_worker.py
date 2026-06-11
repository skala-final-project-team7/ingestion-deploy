"""Ingestion Worker — 수집 job consume 및 completion publish 처리 [Worker 경계].

--------------------------------------------------
작성자 : 이다연
작성목적 : ``/ml/ingest`` API 또는 향후 RabbitMQ ingest 큐 consumer 가 공통적으로 재사용할
          수집 종료(COMPLETED/FAILED) 처리 경로를 한 곳에 정리한다. 수집 수명주기를
          terminal 상태로 마감하고, 완료 이벤트를 안전하게 발행해 BFF Admin Key deactivate
          트리거를 보장한다.
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-06-11, featureI-7c — success/failed publish 분기를 이 파일에 정규화.
--------------------------------------------------
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import logging

from app.api.deps import IngestDeps
from app.api.ingest_completion import IngestCompletionEvent, publish_ingest_completion_safely
from app.ingestion.crawler import CrawlRequest
from app.ingestion.sync import DeltaSyncRequest
from app.schemas.enums import IngestJobStatus

_LOGGER = logging.getLogger(__name__)

CredentialLookup = Callable[[str], tuple[str | None, str | None]]


def _resolve_runtime_credentials(
    admin_user_id: str | None,
    *,
    access_token: str | None,
    cloud_id: str | None,
    credential_lookup: CredentialLookup | None,
) -> tuple[str | None, str | None]:
    """adminUserId 기반 credential 조회를 우선 시도하되, 조회 실패 시 request 값으로 fallback 한다.

    현재 단계에서는 auth-server 미구현 구간이 존재하므로, 조회 실패 시 예외를 전파하지 않고
    기존 credential(또는 None)을 그대로 사용한다.
    """
    if credential_lookup is None or not admin_user_id:
        return access_token, cloud_id

    try:
        resolved_access_token, resolved_cloud_id = credential_lookup(admin_user_id)
    except Exception:
        _LOGGER.exception("admin credential lookup failed: admin_user_id=%s", admin_user_id)
        return access_token, cloud_id

    return resolved_access_token or access_token, resolved_cloud_id or cloud_id


def publish_ingest_completion(
    deps: IngestDeps,
    *,
    job_id: str,
    mode: str,
    status: IngestJobStatus,
    admin_user_id: str | None,
    finished_at: datetime,
    error_code: str | None = None,
    error: str | None = None,
) -> None:
    """terminal 수집 완료 후 completion event 를 발행한다."""
    publish_ingest_completion_safely(
        deps.completion_publisher,
        IngestCompletionEvent(
            job_id=job_id,
            mode=mode,
            status=status,
            admin_user_id=admin_user_id,
            error_code=error_code,
            message=error,
            completed_at=finished_at,
        ),
    )


def run_ingest_job(
    deps: IngestDeps,
    job_id: str,
    mode: str,
    crawl_request: CrawlRequest,
    *,
    credential_lookup: CredentialLookup | None = None,
) -> None:
    """수집 잡을 실행하고 terminal 상태(COMPLETED/FAILED)로 정리한다."""
    deps.job_store.update(job_id, status=IngestJobStatus.IN_PROGRESS)

    access_token, cloud_id = _resolve_runtime_credentials(
        crawl_request.admin_user_id,
        access_token=crawl_request.access_token,
        cloud_id=crawl_request.cloud_id,
        credential_lookup=credential_lookup,
    )

    request = CrawlRequest(
        space_key=crawl_request.space_key,
        admin_user_id=crawl_request.admin_user_id,
        access_token=access_token,
        cloud_id=cloud_id,
    )

    try:
        result = deps.run_crawl(request)
    except Exception as exc:  # noqa: BLE001 — 잡 단위 예외 격리
        finished_at = datetime.now(UTC)
        _LOGGER.exception("ingest job failed: job_id=%s", job_id)
        deps.job_store.update(
            job_id,
            status=IngestJobStatus.FAILED,
            finished_at=finished_at,
            error=str(exc),
        )
        publish_ingest_completion(
            deps,
            job_id=job_id,
            mode=mode,
            status=IngestJobStatus.FAILED,
            admin_user_id=crawl_request.admin_user_id,
            error_code="INGEST_FAILED",
            error=str(exc),
            finished_at=finished_at,
        )
        return

    finished_at = datetime.now(UTC)
    failed = len(result.failed_page_ids)
    deps.job_store.update(
        job_id,
        status=IngestJobStatus.COMPLETED,
        total_pages=result.pages_collected + failed,
        processed_pages=result.pages_collected,
        failed_pages=failed,
        finished_at=finished_at,
    )
    publish_ingest_completion(
        deps,
        job_id=job_id,
        mode=mode,
        status=IngestJobStatus.COMPLETED,
        admin_user_id=crawl_request.admin_user_id,
        finished_at=finished_at,
    )


def run_delta_ingest_job(
    deps: IngestDeps,
    job_id: str,
    delta_request: DeltaSyncRequest,
    *,
    credential_lookup: CredentialLookup | None = None,
) -> None:
    """Delta Sync 잡을 실행하고 terminal 상태(COMPLETED/FAILED)로 정리한다."""
    deps.job_store.update(job_id, status=IngestJobStatus.IN_PROGRESS)

    access_token, cloud_id = _resolve_runtime_credentials(
        delta_request.admin_user_id,
        access_token=delta_request.access_token,
        cloud_id=delta_request.cloud_id,
        credential_lookup=credential_lookup,
    )

    request = DeltaSyncRequest(
        previous_snapshot_path=delta_request.previous_snapshot_path,
        access_token=access_token,
        cloud_id=cloud_id,
        admin_user_id=delta_request.admin_user_id,
    )

    try:
        result = deps.run_delta(request)
    except Exception as exc:  # noqa: BLE001 — 잡 단위 예외 격리
        finished_at = datetime.now(UTC)
        _LOGGER.exception("delta ingest job failed: job_id=%s", job_id)
        deps.job_store.update(
            job_id,
            status=IngestJobStatus.FAILED,
            finished_at=finished_at,
            error=str(exc),
        )
        publish_ingest_completion(
            deps,
            job_id=job_id,
            mode="delta",
            status=IngestJobStatus.FAILED,
            admin_user_id=delta_request.admin_user_id,
            error_code="INGEST_FAILED",
            error=str(exc),
            finished_at=finished_at,
        )
        return

    finished_at = datetime.now(UTC)
    delete_result = deps.sync_worker.apply_delta_deletions(
        result, confirm=deps.delta_delete_confirm
    )
    if delete_result.has_failures:
        _LOGGER.warning(
            "delta soft-delete partial failure: job_id=%s deleted=%d failed_pages=%d",
            job_id,
            delete_result.total_soft_deleted,
            len(delete_result.failed_page_ids),
        )
    elif delete_result.total_soft_deleted:
        _LOGGER.info(
            "delta soft-delete applied: job_id=%s deleted=%d",
            job_id,
            delete_result.total_soft_deleted,
        )

    deps.job_store.update(
        job_id,
        status=IngestJobStatus.COMPLETED,
        total_pages=result.changed_pages + result.failed_items,
        processed_pages=result.changed_pages,
        failed_pages=result.failed_items,
        finished_at=finished_at,
    )
    publish_ingest_completion(
        deps,
        job_id=job_id,
        mode="delta",
        status=IngestJobStatus.COMPLETED,
        admin_user_id=delta_request.admin_user_id,
        finished_at=finished_at,
    )

