"""Ingestion Worker 단위 테스트 — completion 이벤트 발행 정합성 검증.

Full/Delta 수집 종료 경로가 spec v2.5.0 completion 계약을 준수해
COMPLETED/FAILED status 를 발행하는지, payload 에는 민감 정보가 제외되는지 확인한다.
"""

from __future__ import annotations

import pytest

from app.api.deps import IngestDeps
from app.api.ingest_completion import IngestCompletionEvent
from app.ingestion.crawler import CrawlRequest, CrawlResult
from app.ingestion.soft_delete import SoftDeleteResult
from app.ingestion.sync import DeltaSyncRequest, DeltaSyncResult
from app.ingestion.workers.ingestion_worker import run_delta_ingest_job, run_ingest_job
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
