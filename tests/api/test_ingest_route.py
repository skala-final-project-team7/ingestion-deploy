"""수집 HTTP API 라우트 회귀 — POST /ml/ingest + status + health.

본 테스트는 api-spec v2.2.0 §2-2/§2-3/§2-4-2 계약을 검증한다.
- POST /ml/ingest → jobId 발급 + status=STARTED + startedAt(KST), 백그라운드 크롤 후 COMPLETED.
- GET /ml/ingest/status/{jobId} → jobId/status/totalPages/processedPages/failedPages/startedAt.
- GET /ml/ingest/health → {"status": "UP"}.
- (v2.5.0) terminal(COMPLETED/FAILED) 상태에서 credential 없는 RabbitMQ completion event 발행
  + adminUserId 식별자.
- (FR-005) mode=delta 는 run_crawl 이 아니라 run_delta(Delta Sync)로 분기·카운트 보고.

크롤 러너는 stub 으로 주입해(외부 컨테이너·샘플 파일 의존 없이) 잡 카운트 집계만 결정론적으로
검증한다. ASGITransport 는 응답 완료 전에 BackgroundTasks 를 끝내므로 POST 직후 상태 조회 시
이미 COMPLETED 다.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.api.deps import DeltaRunner, IngestDeps
from app.api.ingest_completion import IngestCompletionPublisher, QueueIngestCompletionPublisher
from app.api.main import create_app
from app.api.routes import get_deps
from app.ingestion.crawler import CrawlRequest, CrawlResult
from app.ingestion.sync import DeltaSyncRequest, DeltaSyncResult
from app.ingestion.workers.publisher import FakeQueuePublisher
from app.ingestion.workers.sync_worker import SyncWorker, SyncWorkerDeps
from app.storage.ingest_jobs import InMemoryIngestJobStore
from app.storage.qdrant_fake import FakeQdrantPoolStore


class _RecordingSoftDeleteStore:
    """soft-delete 호출만 기록하는 최소 store(SoftDeleteStore Protocol 충족) — 적용 검증용."""

    def __init__(self) -> None:
        self.deleted_pages: list[str] = []
        self.deleted_attachments: list[str] = []

    def soft_delete_by_page_id(self, page_id: str) -> None:
        self.deleted_pages.append(page_id)

    def soft_delete_by_attachment_id(self, attachment_id: str) -> None:
        self.deleted_attachments.append(attachment_id)


def _stub_deps(
    *,
    completion_publisher: IngestCompletionPublisher | None = None,
    fail: bool = False,
    run_delta: DeltaRunner | None = None,
    sync_worker: SyncWorker | None = None,
    delta_delete_confirm: bool = False,
) -> IngestDeps:
    """stub 크롤/델타 러너를 가진 IngestDeps — 카운트 집계 결정론.

    - 크롤 stub: 3 성공 + 1 실패. ``fail=True`` 면 크롤이 예외를 던진다(full FAILED 경로, 또는
      delta 분기가 크롤을 호출하지 않음을 보장).
    - ``run_delta`` 미지정 시 IngestDeps 기본 PoC(변경분 없음) 러너; 지정 시 delta 분기 검증에 사용.
    - ``sync_worker``/``delta_delete_confirm`` 으로 delta 삭제 후보 soft-delete 적용을 검증한다.
    - ``completion_publisher`` 주입 시 terminal completion event 발행을 검증할 수 있다.
    """

    def _run_crawl(request: CrawlRequest) -> CrawlResult:
        if fail:
            raise RuntimeError("crawl boom")
        return CrawlResult(
            space_key=request.space_key,
            pages_collected=3,
            failed_page_ids=["p-bad"],
        )

    delta_kwargs: dict[str, DeltaRunner] = {}
    if run_delta is not None:
        delta_kwargs["run_delta"] = run_delta
    return IngestDeps(
        job_store=InMemoryIngestJobStore(),
        run_crawl=_run_crawl,
        sync_worker=sync_worker or SyncWorker(SyncWorkerDeps(store=FakeQdrantPoolStore())),
        completion_publisher=completion_publisher,
        delta_delete_confirm=delta_delete_confirm,
        **delta_kwargs,
    )


def _client(deps: IngestDeps) -> httpx.AsyncClient:
    """ASGITransport 클라이언트 — get_deps 를 stub 으로 override(lifespan 우회)."""
    app = create_app()
    app.dependency_overrides[get_deps] = lambda: deps
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.asyncio
async def test_ingest_health_returns_up() -> None:
    """api-spec v2.2.0 §2-4-2 — GET /ml/ingest/health → {"status": "UP"}."""
    async with _client(_stub_deps()) as client:
        resp = await client.get("/ml/ingest/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "UP"}


@pytest.mark.asyncio
async def test_ingest_trigger_then_status_completed() -> None:
    """POST /ml/ingest → STARTED + jobId, 백그라운드 완료 후 status=COMPLETED + 카운트 집계."""
    deps = _stub_deps()
    async with _client(deps) as client:
        # api-spec v2.4.0 §2-2 — spaceKey 없음. mode 만(또는 빈 본문)으로 전체 스페이스 수집.
        resp = await client.post("/ml/ingest", json={"mode": "full"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "STARTED"
        assert body["jobId"].startswith("job-")
        assert body["startedAt"].endswith("+09:00")  # KST 절대 전환
        job_id = body["jobId"]

        # ASGITransport 는 응답 완료 전 BackgroundTasks 를 끝내므로 이미 COMPLETED.
        status_resp = await client.get(f"/ml/ingest/status/{job_id}")
    assert status_resp.status_code == 200
    status = status_resp.json()
    assert status["jobId"] == job_id
    assert status["status"] == "COMPLETED"
    assert status["totalPages"] == 4  # 3 성공 + 1 실패
    assert status["processedPages"] == 3
    assert status["failedPages"] == 1
    assert status["startedAt"].endswith("+09:00")


@pytest.mark.asyncio
async def test_ingest_status_unknown_job_returns_404_envelope() -> None:
    """존재하지 않는 jobId → 4필드 에러 봉투(isSuccess/code/errorCode/message)로 404."""
    async with _client(_stub_deps()) as client:
        resp = await client.get("/ml/ingest/status/job-does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body == {
        "isSuccess": False,
        "code": 404,
        "errorCode": "RESOURCE_NOT_FOUND",
        "message": "수집 작업을 찾을 수 없습니다: job-does-not-exist",
    }


@pytest.mark.asyncio
async def test_ingest_rejects_invalid_mode() -> None:
    """mode 는 full | delta 만 허용 — 그 외 값은 422(Pydantic 검증)."""
    async with _client(_stub_deps()) as client:
        resp = await client.post("/ml/ingest", json={"mode": "bogus"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ingest_accepts_empty_body_no_space_key() -> None:
    """api-spec v2.4.0 §2-2 — spaceKey 제거. 빈 본문도 허용(mode 기본 full, 전체 스페이스 수집)."""
    async with _client(_stub_deps()) as client:
        resp = await client.post("/ml/ingest", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "STARTED"


@pytest.mark.asyncio
async def test_ingest_completed_publishes_completion_event_without_credentials() -> None:
    """api-spec v2.5.0 — 완료 시 completion event 발행(credential 미포함 + adminUserId 식별자)."""
    queue = FakeQueuePublisher()
    deps = _stub_deps(completion_publisher=QueueIngestCompletionPublisher(queue))
    async with _client(deps) as client:
        resp = await client.post(
            "/ml/ingest",
            json={
                "mode": "full",
                "adminUserId": "712020:admin",
                "accessToken": "secret-token",
                "cloudId": "cid-123",
            },
        )
        assert resp.status_code == 200

    assert len(queue.messages) == 1
    msg = queue.messages[0]
    assert msg.routing_key == "ingestion.completed"
    assert msg.body["jobId"].startswith("job-")
    assert msg.body["mode"] == "full"
    assert msg.body["status"] == "COMPLETED"
    assert msg.body["adminUserId"] == "712020:admin"
    # 보안 — credential set 은 절대 completion event payload 에 싣지 않는다(루트 CLAUDE.md).
    assert "accessToken" not in msg.body
    assert "refreshToken" not in msg.body
    assert "cloudId" not in msg.body


@pytest.mark.asyncio
async def test_ingest_failure_publishes_failed_completion_event() -> None:
    """크롤(full) 실패 시 FAILED + errorCode=INGEST_FAILED completion event(credential 미포함)."""
    queue = FakeQueuePublisher()
    deps = _stub_deps(completion_publisher=QueueIngestCompletionPublisher(queue), fail=True)
    async with _client(deps) as client:
        resp = await client.post(
            "/ml/ingest",
            json={"mode": "full", "adminUserId": "712020:admin", "cloudId": "cid-123"},
        )
        assert resp.status_code == 200
        job_id = resp.json()["jobId"]
        status_resp = await client.get(f"/ml/ingest/status/{job_id}")

    assert status_resp.json()["status"] == "FAILED"
    assert len(queue.messages) == 1
    msg = queue.messages[0]
    assert msg.body["mode"] == "full"
    assert msg.body["status"] == "FAILED"
    assert msg.body["errorCode"] == "INGEST_FAILED"
    assert "cloudId" not in msg.body


@pytest.mark.asyncio
async def test_ingest_without_publisher_still_completes() -> None:
    """completion_publisher 미주입(기본 None)이어도 잡은 정상 COMPLETED 된다(발행 no-op)."""
    deps = _stub_deps()  # completion_publisher=None
    async with _client(deps) as client:
        resp = await client.post("/ml/ingest", json={"mode": "full"})
        job_id = resp.json()["jobId"]
        status_resp = await client.get(f"/ml/ingest/status/{job_id}")
    assert status_resp.json()["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_ingest_delta_runs_delta_runner_and_reports_counts() -> None:
    """FR-005 — mode=delta 는 run_delta(Delta Sync)로 분기하고 delta 카운트를 status 에 반영한다."""
    queue = FakeQueuePublisher()

    def _run_delta(request: DeltaSyncRequest) -> DeltaSyncResult:
        return DeltaSyncResult(
            changed_pages=2,
            deleted_candidate_page_ids=["d1", "d2"],
            failed_items=1,
        )

    # fail=True → run_crawl 이 호출되면 예외. delta 분기가 COMPLETED 되면 크롤을 안 탔다는 증거.
    deps = _stub_deps(
        completion_publisher=QueueIngestCompletionPublisher(queue),
        fail=True,
        run_delta=_run_delta,
    )
    async with _client(deps) as client:
        resp = await client.post(
            "/ml/ingest", json={"mode": "delta", "adminUserId": "712020:admin"}
        )
        assert resp.status_code == 200
        job_id = resp.json()["jobId"]
        status_resp = await client.get(f"/ml/ingest/status/{job_id}")

    status = status_resp.json()
    assert status["status"] == "COMPLETED"
    assert status["processedPages"] == 2  # changed_pages
    assert status["failedPages"] == 1  # failed_items
    assert status["totalPages"] == 3  # changed + failed (삭제 후보는 status 미반영)
    assert len(queue.messages) == 1
    msg = queue.messages[0]
    assert msg.body["mode"] == "delta"
    assert msg.body["status"] == "COMPLETED"
    assert msg.body["adminUserId"] == "712020:admin"
    assert "cloudId" not in msg.body


@pytest.mark.asyncio
async def test_ingest_delta_failure_publishes_failed_completion_event() -> None:
    """delta 실행 실패 시 status=FAILED + errorCode=INGEST_FAILED completion event(mode=delta)."""
    queue = FakeQueuePublisher()

    def _run_delta(request: DeltaSyncRequest) -> DeltaSyncResult:
        raise RuntimeError("delta boom")

    deps = _stub_deps(
        completion_publisher=QueueIngestCompletionPublisher(queue),
        run_delta=_run_delta,
    )
    async with _client(deps) as client:
        resp = await client.post(
            "/ml/ingest", json={"mode": "delta", "adminUserId": "712020:admin"}
        )
        assert resp.status_code == 200
        job_id = resp.json()["jobId"]
        status_resp = await client.get(f"/ml/ingest/status/{job_id}")

    assert status_resp.json()["status"] == "FAILED"
    assert len(queue.messages) == 1
    msg = queue.messages[0]
    assert msg.body["mode"] == "delta"
    assert msg.body["status"] == "FAILED"
    assert msg.body["errorCode"] == "INGEST_FAILED"


@pytest.mark.asyncio
async def test_ingest_delta_applies_soft_delete_when_confirmed() -> None:
    """FR-005 — delta_delete_confirm=True 면 삭제 후보를 apply_delta_deletions 로 soft-delete."""
    store = _RecordingSoftDeleteStore()

    def _run_delta(request: DeltaSyncRequest) -> DeltaSyncResult:
        return DeltaSyncResult(
            changed_pages=1,
            deleted_candidate_page_ids=["P2", "P9"],
            failed_items=0,
        )

    deps = _stub_deps(
        run_delta=_run_delta,
        sync_worker=SyncWorker(SyncWorkerDeps(store=store)),
        delta_delete_confirm=True,
    )
    async with _client(deps) as client:
        resp = await client.post(
            "/ml/ingest", json={"mode": "delta", "adminUserId": "712020:admin"}
        )
        job_id = resp.json()["jobId"]
        status_resp = await client.get(f"/ml/ingest/status/{job_id}")

    assert status_resp.json()["status"] == "COMPLETED"
    assert store.deleted_pages == ["P2", "P9"]  # apply_soft_deletes 정규화(정렬·dedup)


@pytest.mark.asyncio
async def test_ingest_delta_does_not_soft_delete_when_not_confirmed() -> None:
    """기본(confirm=False)은 삭제 후보를 적용하지 않는다(surface-only 정책 보존)."""
    store = _RecordingSoftDeleteStore()

    def _run_delta(request: DeltaSyncRequest) -> DeltaSyncResult:
        return DeltaSyncResult(changed_pages=1, deleted_candidate_page_ids=["P2"], failed_items=0)

    deps = _stub_deps(run_delta=_run_delta, sync_worker=SyncWorker(SyncWorkerDeps(store=store)))
    async with _client(deps) as client:
        resp = await client.post(
            "/ml/ingest", json={"mode": "delta", "adminUserId": "712020:admin"}
        )
        job_id = resp.json()["jobId"]
        status_resp = await client.get(f"/ml/ingest/status/{job_id}")

    assert status_resp.json()["status"] == "COMPLETED"
    assert store.deleted_pages == []  # confirm=False → 미적용
