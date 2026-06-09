"""run_delta_sync 단위 테스트 — vendored Data Sync Agent 통합 경계 검증.

vendored delta sync workflow 는 fake runner 로 대체하고, ChangedDocument→PageObject
매핑·raw_pages 적재·Chunking Queue 재투입·삭제 후보 집계를 검증한다. 기존
reconcile_deletions 는 본 테스트에서 건드리지 않는다(무수정 보존).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.ingestion.sync import DeltaSyncRequest, run_delta_sync
from app.ingestion.workers import QUEUE_CHUNKING
from app.ingestion.workers.publisher import FakeQueuePublisher
from app.storage.raw_store import FakeRawPageStore


def _changed(page_id: str, *, space_key: str = "ENG", version: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        space={"space_id": "s1", "space_key": space_key, "space_name": "Engineering"},
        page={
            "page_id": page_id,
            "title": f"Title {page_id}",
            "page_url": f"/wiki/{page_id}",
            "last_modified_at": "2026-05-14T01:00:00Z",
            "version_number": version,
        },
        body={
            "representation": "storage",
            "storage_html": f"<p>{page_id}</p>",
            "plain_text": page_id,
        },
    )


def _fake_runner(
    *,
    changed: list[SimpleNamespace],
    deleted: list[SimpleNamespace],
    failed: list[Any],
):
    def runner(
        *,
        config: Any,
        client: Any | None = None,
        snapshot_repository: Any | None = None,
        force_sequential: bool = False,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            changed_documents=changed,
            deleted_items=deleted,
            failed_items=failed,
        )

    return runner


def _request() -> DeltaSyncRequest:
    return DeltaSyncRequest(
        previous_snapshot_path="/tmp/previous_snapshot.json",
        cloud_id="cloud-synthetic",
        access_token="token-synthetic",
    )


def test_run_delta_sync_reingests_changed_pages_and_collects_deletes() -> None:
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    runner = _fake_runner(
        changed=[_changed("page-1", version=4)],
        deleted=[SimpleNamespace(page_id="page-9"), SimpleNamespace(page_id="page-3")],
        failed=[],
    )

    result = run_delta_sync(
        _request(),
        raw_store=store,
        publisher=publisher,
        workflow_runner=runner,
    )

    assert result.changed_pages == 1
    assert set(store.pages) == {"page-1"}
    assert result.deleted_candidate_page_ids == ["page-3", "page-9"]
    assert result.failed_items == 0

    page = store.pages["page-1"]
    assert page.space_key == "ENG"
    assert page.body_html == "<p>page-1</p>"
    assert page.version_number == 4
    assert page.allowed_groups == ["space:ENG"]

    assert [m.routing_key for m in publisher.messages] == [QUEUE_CHUNKING]
    assert publisher.messages[0].body == {
        "page_id": "page-1",
        "space_key": "ENG",
        "version_number": 4,
        "source_type": "page",
    }


def test_run_delta_sync_filters_by_requested_space_key() -> None:
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    runner = _fake_runner(
        changed=[_changed("page-1", space_key="ENG"), _changed("page-2", space_key="OPS")],
        deleted=[],
        failed=[],
    )

    request = DeltaSyncRequest(
        previous_snapshot_path="/tmp/previous_snapshot.json",
        space_key="ENG",
        cloud_id="cloud-synthetic",
        access_token="token-synthetic",
    )
    result = run_delta_sync(request, raw_store=store, publisher=publisher, workflow_runner=runner)

    assert result.changed_pages == 1
    assert set(store.pages) == {"page-1"}
    assert len(publisher.messages) == 1


def test_run_delta_sync_counts_failed_items() -> None:
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    runner = _fake_runner(
        changed=[],
        deleted=[],
        failed=[SimpleNamespace(item_id="page-x"), SimpleNamespace(item_id="page-y")],
    )

    result = run_delta_sync(
        _request(), raw_store=store, publisher=publisher, workflow_runner=runner
    )

    assert result.changed_pages == 0
    assert result.deleted_candidate_page_ids == []
    assert result.failed_items == 2
