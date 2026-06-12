"""InMemoryIngestJobStore 회귀 — 스냅샷 반환 + 외부 jobId 수용 (배포 전 점검 2026-06-10).

작성자 : 최태성
담당 영역 : ingestion

- ``get``/``update``/``create`` 는 내부 레코드의 **스냅샷**을 반환해야 한다. 라이브 객체를
  반환하면 백그라운드 태스크의 필드별 setattr 와 상태 조회 라우트의 직렬화가 같은 객체에서
  교차해 torn read(예: COMPLETED 인데 카운트 0)가 된다.
- ``create(job_id=...)`` 는 외부(BFF)가 생성한 작업 식별자를 그대로 사용한다(api-spec
  v2.5.0 §2-2 — completion event/status/deactivate idempotency 의 기준 키).
"""

from __future__ import annotations

from app.schemas.enums import IngestJobStatus
from app.storage.ingest_jobs import InMemoryIngestJobStore


def test_create_honors_external_job_id() -> None:
    store = InMemoryIngestJobStore()
    record = store.create(job_id="job-bff-7")
    assert record.job_id == "job-bff-7"
    fetched = store.get("job-bff-7")
    assert fetched is not None
    assert fetched.status is IngestJobStatus.STARTED


def test_create_generates_id_when_absent() -> None:
    record = InMemoryIngestJobStore().create()
    assert record.job_id.startswith("job-")


def test_get_returns_snapshot_not_live_record() -> None:
    store = InMemoryIngestJobStore()
    job_id = store.create().job_id

    snapshot = store.get(job_id)
    assert snapshot is not None
    # 반환 객체를 변조해도 저장소 내부 상태는 오염되지 않는다(공유 객체 아님).
    snapshot.status = IngestJobStatus.FAILED
    snapshot.total_pages = 999

    fresh = store.get(job_id)
    assert fresh is not None
    assert fresh.status is IngestJobStatus.STARTED
    assert fresh.total_pages == 0


def test_update_returns_snapshot_and_applies_changes() -> None:
    store = InMemoryIngestJobStore()
    job_id = store.create().job_id

    updated = store.update(job_id, status=IngestJobStatus.COMPLETED, total_pages=4)
    assert updated is not None
    assert updated.status is IngestJobStatus.COMPLETED
    assert updated.total_pages == 4

    # update 반환 스냅샷 변조도 내부 상태와 분리된다.
    updated.total_pages = -1
    fresh = store.get(job_id)
    assert fresh is not None
    assert fresh.total_pages == 4


def test_update_unknown_job_returns_none() -> None:
    assert InMemoryIngestJobStore().update("job-none", status=IngestJobStatus.FAILED) is None
