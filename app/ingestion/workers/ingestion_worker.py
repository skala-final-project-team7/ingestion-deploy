"""Ingestion Worker — 수집 job consume 및 completion publish 처리 [Worker 경계].

--------------------------------------------------
작성자 : 이다연
작성목적 : ``/ml/ingest`` API 또는 향후 RabbitMQ ingest 큐 consumer 가 공통적으로 재사용할
          수집 종료(COMPLETED/FAILED) 처리 경로를 한 곳에 정리한다. 수집 수명주기를 terminal
          상태로 마감하고, 완료 이벤트를 안전하게 발행해 BFF Admin Key deactivate 트리거를
          보장한다.
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-06-11, featureI-7c — success/failed publish 분기를 이 파일에 정규화.
  - 2026-06-11, featureI-7c Step 4 — 실패 경로에서 completion 이벤트 강제 보장.
    수집/Delta 실패 시 `FAILED` 발행을 상태 저장 실패와 분리해 `publish 실패`만 제외하고
    보장하도록 `_publish_failed_completion` 경로를 추가한다. `run_delta_ingest_job`는 삭제 반영
    예외도 이 경로로 수렴해 BFF deactivate 트리거가 중단되지 않도록 정합화한다.
--------------------------------------------------
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from app.api.deps import IngestDeps
from app.api.ingest_completion import IngestCompletionEvent, publish_ingest_completion_safely
from app.ingestion.crawler import CrawlRequest
from app.ingestion.progress import IngestProgress, IngestProgressCallback
from app.ingestion.sync import DeltaSyncRequest
from app.schemas.enums import IngestJobStatus

_LOGGER = logging.getLogger(__name__)

CredentialLookup = Callable[[str], tuple[str | None, str | None]]
_TERMINAL_STATUSES = frozenset({IngestJobStatus.COMPLETED, IngestJobStatus.FAILED})


def _build_job_progress_callback(deps: IngestDeps, job_id: str) -> IngestProgressCallback:
    """Agent progress 이벤트를 `/ml/ingest/status` 카운터에 반영한다."""

    def _callback(progress: IngestProgress) -> None:
        record = deps.job_store.get(job_id)
        if record is not None and record.status in _TERMINAL_STATUSES:
            return

        updates: dict[str, object] = {"status": IngestJobStatus.IN_PROGRESS}
        _copy_non_negative_int(
            progress,
            updates,
            source_key="total_pages",
            target_key="total_pages",
        )
        _copy_non_negative_int(
            progress,
            updates,
            source_key="processed_pages",
            target_key="processed_pages",
        )
        _copy_non_negative_int(
            progress,
            updates,
            source_key="failed_pages",
            target_key="failed_pages",
        )
        if len(updates) > 1:
            deps.job_store.update(job_id, **updates)

    return _callback


def _copy_non_negative_int(
    progress: IngestProgress,
    updates: dict[str, object],
    *,
    source_key: str,
    target_key: str,
) -> None:
    value = progress.get(source_key)
    if isinstance(value, int):
        updates[target_key] = max(0, value)


@dataclass(frozen=True, slots=True)
class IngestJobCommand:
    """BFF가 RabbitMQ로 발행한 수집 job 계약."""

    job_id: str
    admin_user_id: str
    mode: str
    requested_at: datetime

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> IngestJobCommand:
        """필수 필드(``jobId, adminUserId, mode, requestedAt``)를 검증해 파싱한다."""
        missing = {"jobId", "adminUserId", "mode", "requestedAt"} - set(payload.keys())
        if missing:
            raise ValueError(f"invalid ingest job payload: missing {sorted(missing)}")

        job_id = str(payload["jobId"]).strip() if isinstance(payload["jobId"], str) else ""
        admin_user_id = (
            str(payload["adminUserId"]).strip() if isinstance(payload["adminUserId"], str) else ""
        )
        mode = str(payload["mode"]).strip()
        if not job_id:
            raise ValueError("jobId is required")
        if mode not in {"full", "delta"}:
            raise ValueError(f"invalid mode={mode!r}; expected full|delta")
        if not admin_user_id:
            raise ValueError("adminUserId is required")

        raw_requested_at = payload["requestedAt"]
        if isinstance(raw_requested_at, datetime):
            requested_at = raw_requested_at
        elif isinstance(raw_requested_at, str):
            requested_at = _parse_requested_at(raw_requested_at)
        else:
            raise ValueError("requestedAt is required and must be ISO-8601 string")
        if requested_at.tzinfo is None:
            requested_at = requested_at.replace(tzinfo=UTC)

        return cls(
            job_id=job_id,
            admin_user_id=admin_user_id,
            mode=mode,
            requested_at=requested_at,
        )


def _parse_requested_at(requested_at: str) -> datetime:
    """BFF 계약의 ISO-8601 ``requestedAt`` 값을 파싱한다(naive 인입은 UTC로 해석)."""
    normalized = requested_at.strip().replace("Z", "+00:00")
    if not normalized:
        raise ValueError("requestedAt is required and must be ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("requestedAt must be ISO-8601 string") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _resolve_runtime_credentials(
    admin_user_id: str | None,
    *,
    access_token: str | None,
    cloud_id: str | None,
    credential_lookup: CredentialLookup | None,
) -> tuple[str | None, str | None]:
    """adminUserId 기반 credential 조회를 우선 시도한다.

    `credential_lookup` 을 통해 auth-server 내부 credential 조회를 수행한다.
    상태 분기:
    - 400: adminUserId 누락/형식 오류
    - 401: INTERNAL_API_KEY 누락 또는 미스매치
    - 403: adminUserId가 ADMIN 아님
    - 404: 사용자 없음/ OAuth 미로그인

    legacy 경로로 토큰이 이미 전달된 경우에는 lookup 실패 시 폴백하고,
    토큰 미전달 모드에서는 상태코드 기반 실패를 즉시 상위로 전파한다.
    """
    if credential_lookup is None or not admin_user_id:
        return access_token, cloud_id

    try:
        resolved_access_token, resolved_cloud_id = credential_lookup(admin_user_id)
    except Exception as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        detail_message = _extract_response_error_message(response)
        has_legacy_tokens = access_token is not None or cloud_id is not None
        should_fallback = has_legacy_tokens and (
            status_code is None
            or status_code in {400, 401, 403, 404}
            or (isinstance(status_code, int) and status_code >= 500)
        )

        if status_code == 400:
            _LOGGER.warning(
                "admin credential lookup 400: adminUserId 누락/형식 오류. adminUserId=%s",
                admin_user_id,
            )
        elif status_code == 401:
            if detail_message and "missing" in detail_message:
                _LOGGER.warning(
                    "admin credential lookup 401: INTERNAL_API_KEY 누락. adminUserId=%s",
                    admin_user_id,
                )
            elif detail_message and "mismatch" in detail_message:
                _LOGGER.warning(
                    "admin credential lookup 401: INTERNAL_API_KEY 미스매치. adminUserId=%s",
                    admin_user_id,
                )
            else:
                _LOGGER.error(
                    "admin credential lookup 401: INTERNAL_API_KEY 누락/미스매치 확인 필요. "
                    "adminUserId=%s",
                    admin_user_id,
                )
        elif status_code == 403:
            _LOGGER.warning(
                "admin credential lookup 403: adminUserId가 ADMIN 권한이 아님. adminUserId=%s",
                admin_user_id,
            )
        elif status_code == 404:
            _LOGGER.warning(
                "admin credential lookup 404: 해당 adminUserId가 없거나 OAuth 로그인 전 상태. "
                "adminUserId=%s",
                admin_user_id,
            )
        elif status_code is not None and status_code >= 500:
            _LOGGER.warning(
                "admin credential lookup %s: auth-server 처리 실패. "
                "재시도 또는 장애 전파 정책으로 처리. adminUserId=%s",
                status_code,
                admin_user_id,
            )
        elif status_code is None:
            _LOGGER.warning(
                "admin credential lookup network/예외 오류. 예외=%s. adminUserId=%s",
                type(exc).__name__,
                admin_user_id,
            )
        else:
            _LOGGER.exception(
                "admin credential lookup failed: adminUserId=%s (status_code=%s)",
                admin_user_id,
                status_code,
            )

        if should_fallback:
            _LOGGER.info(
                "admin credential lookup 실패로 legacy credential fallback 수행."
                " adminUserId=%s status=%s",
                admin_user_id,
                status_code,
            )
            return access_token, cloud_id

        if status_code is None:
            raise RuntimeError("admin credential lookup failed with non-http error") from exc

        raise RuntimeError(f"admin credential lookup failed: status_code={status_code}") from exc

    return resolved_access_token or access_token, resolved_cloud_id or cloud_id


def _extract_response_error_message(response: object | None) -> str | None:
    """auth-server 오류 본문에서 사용자 메시지를 추출한다.

    auth-server 테스트 더블에서 body 가 JSON인지, plain text인지 구분해 메시지만 안전하게
    추출한다. 파싱 실패 시 raw text 를 반환한다.
    """
    if response is None:
        return None
    text = getattr(response, "text", None)
    if not isinstance(text, str):
        return None

    body = text.strip()
    if not body:
        return None

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body

    if isinstance(payload, dict):
        for key in ("message", "error", "detail", "description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return body.lower()


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
    """terminal 수집 종료 상태를 completion 이벤트로 publish한다.

    별도 예외 처리 없이 publish seam 에 위임한다.
    이벤트 payload 구성은 DTO에서 필드 허용/제한 정책(민감정보 제외)을 보장한다.
    """
    publish_ingest_completion_safely(
        deps.completion_publisher,
        IngestCompletionEvent(
            job_id=job_id,
            mode=mode,
            status=status,
            admin_user_id=admin_user_id or "",
            error_code=error_code,
            message=error,
            completed_at=finished_at,
        ),
    )


def _record_sync_log(
    deps: IngestDeps,
    *,
    job_id: str,
    mode: str,
    status: IngestJobStatus,
    started_at: datetime | None,
    finished_at: datetime,
    sync_id: str | None = None,
    updated_pages: int = 0,
    deleted_pages: int = 0,
    failed_pages: int = 0,
    raw_status: str | None = None,
    error: str | None = None,
    metadata: Mapping[str, object] | None = None,
) -> None:
    """관리자 대시보드용 ``sync_logs`` 기록을 시도한다."""
    repository = getattr(deps, "sync_log_repository", None)
    if repository is None:
        return

    resolved_started_at = started_at or finished_at
    duration_seconds = max(0, round((finished_at - resolved_started_at).total_seconds()))
    document: dict[str, object] = {
        "syncId": sync_id or job_id,
        "jobId": job_id,
        "mode": mode,
        "status": status.value,
        "updatedPages": max(0, updated_pages),
        "deletedPages": max(0, deleted_pages),
        "failedPages": max(0, failed_pages),
        "startedAt": resolved_started_at,
        "completedAt": finished_at,
        "duration": duration_seconds,
    }
    if raw_status:
        document["rawStatus"] = raw_status
    if error:
        document["error"] = error
    if metadata:
        document["metadata"] = dict(metadata)

    try:
        repository.record(document)
    except Exception:  # noqa: BLE001 — 관리자 이력 기록 실패가 수집 terminal 처리를 막지 않음
        _LOGGER.exception("sync log persistence failed: job_id=%s mode=%s", job_id, mode)


def _publish_failed_completion(
    deps: IngestDeps,
    *,
    job_id: str,
    mode: str,
    admin_user_id: str | None,
    error: Exception,
    started_at: datetime | None = None,
    sync_id: str | None = None,
    updated_pages: int = 0,
    deleted_pages: int = 0,
    failed_pages: int = 0,
) -> None:
    """실패 잡 종료 경로에서 completion 이벤트를 보장 발행한다.

    구현 포인트:
    - `job_store.update`가 실패해도(디스크/네트워크/락 경합) 잡 상태 기록 실패로
      completion publish 가 누락되지 않도록 먼저 고립 처리.
    - publish 본체는 `publish_ingest_completion_safely`를 통해 재시도/예외 흡수를 수행한다.
    """
    finished_at = datetime.now(UTC)
    try:
        deps.job_store.update(
            job_id,
            status=IngestJobStatus.FAILED,
            finished_at=finished_at,
            error=str(error),
        )
    except Exception:  # noqa: BLE001 — 상태 영속화 실패는 이벤트 발행 자체를 막지 않음
        _LOGGER.exception("failed job state persistence failed: job_id=%s", job_id)

    publish_ingest_completion(
        deps,
        job_id=job_id,
        mode=mode,
        status=IngestJobStatus.FAILED,
        admin_user_id=admin_user_id,
        error_code="INGEST_FAILED",
        error=str(error),
        finished_at=finished_at,
    )
    _record_sync_log(
        deps,
        job_id=job_id,
        mode=mode,
        status=IngestJobStatus.FAILED,
        started_at=started_at,
        finished_at=finished_at,
        sync_id=sync_id,
        updated_pages=updated_pages,
        deleted_pages=deleted_pages,
        failed_pages=failed_pages,
        raw_status="failed",
        error=str(error),
    )


def run_ingest_job_from_payload(
    deps: IngestDeps,
    payload: Mapping[str, object],
    *,
    credential_lookup: CredentialLookup | None = None,
) -> None:
    """RabbitMQ ``admin.ingest.requested`` 메시지를 파싱해 full/delta ingest 를 실행한다."""
    command = IngestJobCommand.from_payload(payload)
    existing = deps.job_store.get(command.job_id)
    if existing is not None and existing.status in {
        IngestJobStatus.IN_PROGRESS,
        IngestJobStatus.COMPLETED,
        IngestJobStatus.FAILED,
    }:
        _LOGGER.info(
            "ingest job 중복 수신 skip (idempotent): job_id=%s status=%s",
            command.job_id,
            existing.status.value,
        )
        return

    credential_lookup_callable = credential_lookup or getattr(deps, "credential_lookup", None)
    if command.mode == "delta":
        run_delta_ingest_job(
            deps,
            job_id=command.job_id,
            delta_request=DeltaSyncRequest(
                previous_snapshot_path=deps.previous_snapshot_path,
                admin_user_id=command.admin_user_id,
            ),
            credential_lookup=credential_lookup_callable
            if callable(credential_lookup_callable)
            else None,
        )
        return

    run_ingest_job(
        deps,
        job_id=command.job_id,
        mode=command.mode,
        crawl_request=CrawlRequest(admin_user_id=command.admin_user_id),
        credential_lookup=credential_lookup_callable
        if callable(credential_lookup_callable)
        else None,
    )


def run_ingest_job(
    deps: IngestDeps,
    job_id: str,
    mode: str,
    crawl_request: CrawlRequest,
    *,
    credential_lookup: CredentialLookup | None = None,
) -> None:
    """수집 잡을 실행하고 terminal 상태(COMPLETED/FAILED)로 정리한다.

    실패 시 `_publish_failed_completion`를 호출해 BFF deactivate 트리거용 FAILED 이벤트를
    먼저 보장한다.
    """
    started_at = datetime.now(UTC)
    progress_callback = _build_job_progress_callback(deps, job_id)
    deps.job_store.update(
        job_id,
        status=IngestJobStatus.IN_PROGRESS,
        total_pages=0,
        processed_pages=0,
        failed_pages=0,
    )

    try:
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
            progress_callback=progress_callback,
        )
        result = deps.run_crawl(request)
    except Exception as exc:  # noqa: BLE001 — 잡 단위 예외 격리
        _LOGGER.exception("ingest job failed: job_id=%s", job_id)
        _publish_failed_completion(
            deps,
            job_id=job_id,
            mode=mode,
            admin_user_id=crawl_request.admin_user_id,
            error=exc,
            started_at=started_at,
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
    _record_sync_log(
        deps,
        job_id=job_id,
        mode=mode,
        status=IngestJobStatus.COMPLETED,
        started_at=started_at,
        finished_at=finished_at,
        updated_pages=result.pages_collected,
        failed_pages=failed,
        raw_status="completed",
    )


def run_delta_ingest_job(
    deps: IngestDeps,
    job_id: str,
    delta_request: DeltaSyncRequest,
    *,
    credential_lookup: CredentialLookup | None = None,
) -> None:
    """Delta Sync 잡을 실행하고 terminal 상태(COMPLETED/FAILED)로 정리한다.

    실패 시 `_publish_failed_completion`로 빠르게 FAILED 이벤트로 수렴하고,
    delta 삭제 반영 단계(`apply_delta_deletions`) 예외도 동일 경로로 연결해
    수집 실패라도 deactivate 경로가 누락되지 않게 한다.
    """
    started_at = datetime.now(UTC)
    progress_callback = _build_job_progress_callback(deps, job_id)
    deps.job_store.update(
        job_id,
        status=IngestJobStatus.IN_PROGRESS,
        total_pages=0,
        processed_pages=0,
        failed_pages=0,
    )

    try:
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
            progress_callback=progress_callback,
        )
        result = deps.run_delta(request)
    except Exception as exc:  # noqa: BLE001 — 잡 단위 예외 격리
        _LOGGER.exception("delta ingest job failed: job_id=%s", job_id)
        _publish_failed_completion(
            deps,
            job_id=job_id,
            mode="delta",
            admin_user_id=delta_request.admin_user_id,
            error=exc,
            started_at=started_at,
        )
        return

    finished_at = datetime.now(UTC)
    try:
        delete_result = deps.sync_worker.apply_delta_deletions(
            result, confirm=deps.delta_delete_confirm
        )
    except Exception as exc:  # noqa: BLE001 — 삭제 게이트 실패도 terminal failed 로 전환
        _LOGGER.exception("delta delete apply failed: job_id=%s", job_id)
        _publish_failed_completion(
            deps,
            job_id=job_id,
            mode="delta",
            admin_user_id=delta_request.admin_user_id,
            error=exc,
            started_at=started_at,
            sync_id=result.sync_id,
            updated_pages=result.changed_pages,
            deleted_pages=len(result.deleted_candidate_page_ids),
            failed_pages=result.failed_items,
        )
        return

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
    _record_sync_log(
        deps,
        job_id=job_id,
        mode="delta",
        status=IngestJobStatus.COMPLETED,
        started_at=started_at,
        finished_at=finished_at,
        sync_id=result.sync_id,
        updated_pages=result.changed_pages,
        deleted_pages=len(result.deleted_candidate_page_ids),
        failed_pages=result.failed_items,
        raw_status="completed",
        metadata={
            "softDeletedPages": delete_result.total_soft_deleted,
            "softDeleteFailedPages": len(delete_result.failed_page_ids),
        },
    )
