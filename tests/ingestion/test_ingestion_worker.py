"""Ingestion Worker 단위 테스트 — completion 이벤트 발행 정합성 검증.

Full/Delta 수집 종료 경로가 spec v2.5.0 completion 계약을 준수해
COMPLETED/FAILED status 를 발행하는지, payload 에는 민감 정보가 제외되는지 확인한다.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.api.deps import IngestDeps
from app.api.ingest_completion import IngestCompletionEvent
from app.ingestion.crawler import CrawlRequest, CrawlResult
from app.ingestion.soft_delete import SoftDeleteResult
from app.ingestion.sync import DeltaSyncRequest, DeltaSyncResult
from app.ingestion.workers.ingestion_worker import (
    _resolve_runtime_credentials,
    run_delta_ingest_job,
    run_ingest_job,
)
from app.schemas.enums import IngestJobStatus


class _FakeJobStore:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, object]]] = []

    def update(self, job_id: str, **changes: object) -> None:
        self.updates.append((job_id, dict(changes)))


class _FakeSyncWorker:
    def __init__(self, *, delete_result: SoftDeleteResult | None = None) -> None:
        self.delete_result = delete_result or SoftDeleteResult()
        self.calls: list[tuple[DeltaSyncResult, bool]] = []

    def apply_delta_deletions(self, result: DeltaSyncResult, *, confirm: bool) -> SoftDeleteResult:
        self.calls.append((result, confirm))
        return self.delete_result


class _FakeCompletionPublisher:
    def __init__(self) -> None:
        self.events: list[IngestCompletionEvent] = []

    def publish(self, event: IngestCompletionEvent) -> None:
        self.events.append(event)


class _FlakyJobStore(_FakeJobStore):
    """지정 호출 순번에 대해 update 예외를 발생시키는 테스트 전용 스토어."""

    def __init__(self, *, fail_on_update_calls: set[int]) -> None:
        super().__init__()
        self.fail_on_update_calls = fail_on_update_calls
        self.update_calls = 0

    def update(self, job_id: str, **changes: object) -> None:
        self.update_calls += 1
        if self.update_calls in self.fail_on_update_calls:
            raise RuntimeError("job store update failed intentionally")
        super().update(job_id, **changes)


class _FailingDeltaSyncWorker(_FakeSyncWorker):
    """delta 삭제 적용 단계 예외를 재현하는 테스트 전용 worker."""

    def apply_delta_deletions(self, result: DeltaSyncResult, *, confirm: bool) -> SoftDeleteResult:
        raise RuntimeError("delta delete failed intentionally")


def _http_status_error(status_code: int) -> Exception:
    request = httpx.Request(
        "GET",
        "http://auth-server.local/internal/auth/admin-confluence-credential",
    )
    response = httpx.Response(status_code, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc


def _http_status_error_with_message(status_code: int, message: str) -> Exception:
    request = httpx.Request(
        "GET",
        "http://auth-server.local/internal/auth/admin-confluence-credential",
    )
    response = httpx.Response(
        status_code,
        content=json.dumps({"message": message}),
        request=request,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise RuntimeError(f"expected http status error: {status_code}")


def test_resolve_runtime_credentials_uses_lookup_result_first() -> None:
    access_token, cloud_id = _resolve_runtime_credentials(
        "admin-1",
        access_token="legacy-token",
        cloud_id="legacy-cloud",
        credential_lookup=lambda _: ("resolved-token", "resolved-cloud"),
    )

    assert access_token == "resolved-token"
    assert cloud_id == "resolved-cloud"


def test_resolve_runtime_credentials_uses_payload_credentials_on_internal_lookup_legacy_path() -> None:
    access_token, cloud_id = _resolve_runtime_credentials(
        "admin-1",
        access_token="legacy-token",
        cloud_id="legacy-cloud",
        credential_lookup=lambda _: (_ for _ in ()).throw(ValueError("lookup failed")),
    )

    assert access_token == "legacy-token"
    assert cloud_id == "legacy-cloud"


def test_ingest_completion_event_payload_has_required_fields_and_no_credentials() -> None:
    event = IngestCompletionEvent(job_id="job-1", mode="full", status=IngestJobStatus.COMPLETED, admin_user_id="admin-42")
    payload = event.to_payload()

    assert set(payload.keys()) >= {"jobId", "adminUserId", "mode", "status", "completedAt"}
    assert payload["status"] == "COMPLETED"
    assert payload["adminUserId"] == "admin-42"
    assert payload["mode"] == "full"
    assert "accessToken" not in payload
    assert "refreshToken" not in payload
    assert "cloudId" not in payload
    assert "adminApiToken" not in payload
    assert "adminEmail" not in payload


def test_ingest_completion_event_rejects_invalid_status() -> None:
    with pytest.raises(ValueError, match="status must be COMPLETED or FAILED"):
        IngestCompletionEvent(
            job_id="job-1",
            admin_user_id="admin-1",
            mode="full",
            status=IngestJobStatus.STARTED,  # type: ignore[arg-type]
        )

def test_run_ingest_job_success_publishes_completed_event() -> None:
    store = _FakeJobStore()
    completion_publisher = _FakeCompletionPublisher()
    deps = IngestDeps(
        job_store=store,
        run_crawl=lambda _req: CrawlResult(space_key="CLOUD", pages_collected=3, failed_page_ids=["failed-1"]),
        sync_worker=_FakeSyncWorker(),
        completion_publisher=completion_publisher,
    )

    run_ingest_job(
        deps,
        job_id="job-success",
        mode="full",
        crawl_request=CrawlRequest(admin_user_id="admin-abc"),
    )

    update = store.updates[-1][1]
    assert update == {
        "status": IngestJobStatus.COMPLETED,
        "total_pages": 4,
        "processed_pages": 3,
        "failed_pages": 1,
        "finished_at": update["finished_at"],
    }
    [published] = completion_publisher.events
    payload = published.to_payload()
    assert payload["status"] == "COMPLETED"
    assert payload["jobId"] == "job-success"
    assert payload["adminUserId"] == "admin-abc"
    assert payload["mode"] == "full"
    assert "accessToken" not in payload


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (400, "admin credential lookup 400"),
        (401, "INTERNAL_API_KEY"),
        (403, "admin credential lookup 403"),
        (404, "admin credential lookup 404"),
    ],
)
def test_resolve_runtime_credentials_http_status_by_code_requires_action(
    status_code: int,
    expected: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _lookup(_admin_user_id: str) -> tuple[str, str]:
        raise _http_status_error(status_code)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(
            RuntimeError,
            match=f"admin credential lookup failed: status_code={status_code}",
        ):
            _resolve_runtime_credentials(
                "admin-1",
                access_token=None,
                cloud_id=None,
                credential_lookup=_lookup,
            )

    assert any(expected in rec.message for rec in caplog.records)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("INTERNAL_API_KEY header missing", "누락"),
        ("INTERNAL_API_KEY mismatch", "미스매치"),
    ],
)
def test_resolve_runtime_credentials_401_missing_and_mismatch_log_classification(
    message: str,
    expected: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _lookup(_admin_user_id: str) -> tuple[str, str]:
        raise _http_status_error_with_message(401, message)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="admin credential lookup failed: status_code=401"):
            _resolve_runtime_credentials(
                "admin-1",
                access_token=None,
                cloud_id=None,
                credential_lookup=_lookup,
            )

    assert any(expected in rec.message for rec in caplog.records)


def test_run_ingest_job_fail_fast_when_lookup_missing_internal_key_and_no_legacy_token() -> None:
    store = _FakeJobStore()
    completion_publisher = _FakeCompletionPublisher()
    run_crawl_calls: list[CrawlRequest] = []

    def _run_crawl(request: CrawlRequest) -> CrawlResult:
        run_crawl_calls.append(request)
        return CrawlResult(space_key="CLOUD", pages_collected=1, failed_page_ids=[])

    deps = IngestDeps(
        job_store=store,
        run_crawl=_run_crawl,
        sync_worker=_FakeSyncWorker(),
        completion_publisher=completion_publisher,
    )

    run_ingest_job(
        deps,
        job_id="job-failfast",
        mode="full",
        crawl_request=CrawlRequest(admin_user_id="admin-1"),
        credential_lookup=lambda _admin_user_id: (_ for _ in ()).throw(
            _http_status_error(401),
        ),
    )

    assert not run_crawl_calls
    assert store.updates[0][0] == "job-failfast"
    assert store.updates[0][1]["status"] == IngestJobStatus.IN_PROGRESS
    assert store.updates[-1][1]["status"] == IngestJobStatus.FAILED
    [published] = completion_publisher.events
    payload = published.to_payload()
    assert payload["status"] == "FAILED"
    assert payload["errorCode"] == "INGEST_FAILED"
    assert "admin credential lookup failed" in str(payload["message"])


def test_run_ingest_job_falls_back_to_legacy_tokens_when_lookup_fails() -> None:
    store = _FakeJobStore()
    completion_publisher = _FakeCompletionPublisher()
    run_requests: list[CrawlRequest] = []

    def _run_crawl(request: CrawlRequest) -> CrawlResult:
        run_requests.append(request)
        return CrawlResult(space_key="CLOUD", pages_collected=1, failed_page_ids=[])

    deps = IngestDeps(
        job_store=store,
        run_crawl=_run_crawl,
        sync_worker=_FakeSyncWorker(),
        completion_publisher=completion_publisher,
    )

    run_ingest_job(
        deps,
        job_id="job-legacy-fallback",
        mode="full",
        crawl_request=CrawlRequest(
            admin_user_id="admin-1",
            access_token="legacy-token",
            cloud_id="legacy-cloud",
        ),
        credential_lookup=lambda _admin_user_id: (_ for _ in ()).throw(
            _http_status_error_with_message(401, "INTERNAL_API_KEY missing"),
        ),
    )

    assert run_requests[0].access_token == "legacy-token"
    assert run_requests[0].cloud_id == "legacy-cloud"
    [published] = completion_publisher.events
    assert published.to_payload()["status"] == "COMPLETED"


def test_resolve_runtime_credentials_500_status_logs_and_falls_back_to_payload() -> None:
    access_token, cloud_id = _resolve_runtime_credentials(
        "admin-1",
        access_token="legacy-token",
        cloud_id="legacy-cloud",
        credential_lookup=lambda _: (_ for _ in ()).throw(_http_status_error(500)),
    )

    assert access_token == "legacy-token"
    assert cloud_id == "legacy-cloud"


def test_run_ingest_job_success_publishes_completed_event_without_sensitive_fields() -> None:
    store = _FakeJobStore()
    completion_publisher = _FakeCompletionPublisher()
    deps = IngestDeps(
        job_store=store,
        run_crawl=lambda _req: CrawlResult(space_key="CLOUD", pages_collected=2, failed_page_ids=[]),
        sync_worker=_FakeSyncWorker(),
        completion_publisher=completion_publisher,
    )

    run_ingest_job(
        deps,
        job_id="job-success-sensitive",
        mode="full",
        crawl_request=CrawlRequest(
            admin_user_id="admin-abc",
            access_token="top-secret-token",
            cloud_id="cloud-id-123",
        ),
    )

    payload = completion_publisher.events[0].to_payload()
    assert payload["status"] == "COMPLETED"
    assert payload["jobId"] == "job-success-sensitive"
    assert payload["adminUserId"] == "admin-abc"
    assert payload["mode"] == "full"
    assert "accessToken" not in payload
    assert "refreshToken" not in payload
    assert "cloudId" not in payload
    assert "adminApiToken" not in payload
    assert "adminEmail" not in payload


def test_run_ingest_job_failure_publishes_failed_event() -> None:
    store = _FakeJobStore()
    completion_publisher = _FakeCompletionPublisher()
    deps = IngestDeps(
        job_store=store,
        run_crawl=lambda _req: (_ for _ in ()).throw(RuntimeError("crawl failed")),
        sync_worker=_FakeSyncWorker(),
        completion_publisher=completion_publisher,
    )

    run_ingest_job(
        deps,
        job_id="job-failed",
        mode="full",
        crawl_request=CrawlRequest(admin_user_id="admin-abc"),
    )

    assert store.updates[-1][1]["status"] == IngestJobStatus.FAILED
    assert "crawl failed" in str(store.updates[-1][1]["error"])
    [published] = completion_publisher.events
    payload = published.to_payload()
    assert payload["status"] == "FAILED"
    assert payload["errorCode"] == "INGEST_FAILED"
    assert "crawl failed" in str(payload["message"])
    assert "accessToken" not in payload


def test_run_ingest_job_failure_publishes_failed_event_without_sensitive_fields() -> None:
    store = _FakeJobStore()
    completion_publisher = _FakeCompletionPublisher()
    deps = IngestDeps(
        job_store=store,
        run_crawl=lambda _req: (_ for _ in ()).throw(RuntimeError("crawl failed")),
        sync_worker=_FakeSyncWorker(),
        completion_publisher=completion_publisher,
    )

    run_ingest_job(
        deps,
        job_id="job-failed-sensitive",
        mode="full",
        crawl_request=CrawlRequest(
            admin_user_id="admin-abc",
            access_token="top-secret-token",
            cloud_id="cloud-id-123",
        ),
    )

    payload = completion_publisher.events[0].to_payload()
    assert payload["status"] == "FAILED"
    assert payload["errorCode"] == "INGEST_FAILED"
    assert "crawl failed" in str(payload["message"])
    assert payload["jobId"] == "job-failed-sensitive"
    assert payload["adminUserId"] == "admin-abc"
    assert "accessToken" not in payload
    assert "refreshToken" not in payload
    assert "cloudId" not in payload
    assert "adminApiToken" not in payload
    assert "adminEmail" not in payload


def test_run_ingest_job_failure_still_publishes_failed_event_if_job_update_fails() -> None:
    store = _FlakyJobStore(fail_on_update_calls={2})
    completion_publisher = _FakeCompletionPublisher()
    deps = IngestDeps(
        job_store=store,
        run_crawl=lambda _req: (_ for _ in ()).throw(RuntimeError("crawl failed")),
        sync_worker=_FakeSyncWorker(),
        completion_publisher=completion_publisher,
    )

    run_ingest_job(
        deps,
        job_id="job-failed-store-update",
        mode="full",
        crawl_request=CrawlRequest(admin_user_id="admin-abc"),
    )

    assert len(store.updates) == 1
    assert store.updates[0][1]["status"] == IngestJobStatus.IN_PROGRESS
    [published] = completion_publisher.events
    payload = published.to_payload()
    assert payload["status"] == "FAILED"
    assert payload["errorCode"] == "INGEST_FAILED"
    assert payload["message"] == "crawl failed"
    assert payload["adminUserId"] == "admin-abc"


def test_run_delta_ingest_job_success_publishes_completed_and_invokes_delta_confirm() -> None:
    store = _FakeJobStore()
    completion_publisher = _FakeCompletionPublisher()
    sync_worker = _FakeSyncWorker(
        delete_result=SoftDeleteResult(soft_deleted_page_ids=["p-1", "p-2"])
    )
    deps = IngestDeps(
        job_store=store,
        run_crawl=lambda _req: CrawlResult(space_key="CLOUD"),
        run_delta=lambda _req: DeltaSyncResult(changed_pages=5, failed_items=2),
        sync_worker=sync_worker,
        completion_publisher=completion_publisher,
        delta_delete_confirm=True,
    )

    run_delta_ingest_job(
        deps,
        job_id="job-delta-success",
        delta_request=DeltaSyncRequest(previous_snapshot_path="", admin_user_id="admin-delta"),
    )

    assert sync_worker.calls[0] == (
        DeltaSyncResult(changed_pages=5, failed_items=2),
        True,
    )
    delta_update = store.updates[-1][1]
    assert delta_update == {
        "status": IngestJobStatus.COMPLETED,
        "total_pages": 7,
        "processed_pages": 5,
        "failed_pages": 2,
        "finished_at": delta_update["finished_at"],
    }
    [published] = completion_publisher.events
    payload = published.to_payload()
    assert payload["status"] == "COMPLETED"
    assert payload["adminUserId"] == "admin-delta"
    assert "accessToken" not in payload


def test_run_delta_ingest_job_failure_publishes_failed_event() -> None:
    store = _FakeJobStore()
    completion_publisher = _FakeCompletionPublisher()
    deps = IngestDeps(
        job_store=store,
        run_crawl=lambda _req: CrawlResult(space_key="CLOUD"),
        run_delta=lambda _req: (_ for _ in ()).throw(ValueError("delta failed")),
        sync_worker=_FakeSyncWorker(),
        completion_publisher=completion_publisher,
    )

    run_delta_ingest_job(
        deps,
        job_id="job-delta-failed",
        delta_request=DeltaSyncRequest(previous_snapshot_path="", admin_user_id="admin-delta"),
    )

    assert store.updates[-1][1]["status"] == IngestJobStatus.FAILED
    assert "delta failed" in str(store.updates[-1][1]["error"])
    [published] = completion_publisher.events
    payload = published.to_payload()
    assert payload["status"] == "FAILED"
    assert payload["errorCode"] == "INGEST_FAILED"


def test_run_delta_ingest_job_failure_still_publishes_failed_event_if_delta_delete_fails() -> None:
    store = _FakeJobStore()
    completion_publisher = _FakeCompletionPublisher()
    deps = IngestDeps(
        job_store=store,
        run_crawl=lambda _req: CrawlResult(space_key="CLOUD"),
        run_delta=lambda _req: DeltaSyncResult(changed_pages=5, failed_items=1),
        sync_worker=_FailingDeltaSyncWorker(),
        completion_publisher=completion_publisher,
        delta_delete_confirm=True,
    )

    run_delta_ingest_job(
        deps,
        job_id="job-delta-delete-error",
        delta_request=DeltaSyncRequest(previous_snapshot_path="", admin_user_id="admin-delta"),
    )

    assert len(completion_publisher.events) == 1
    payload = completion_publisher.events[0].to_payload()
    assert payload["status"] == "FAILED"
    assert payload["errorCode"] == "INGEST_FAILED"
    assert payload["message"] == "delta delete failed intentionally"
    assert payload["adminUserId"] == "admin-delta"
