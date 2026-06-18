"""run_delta_sync 단위 테스트 — vendored Data Sync Agent 통합 경계 검증.

작성자 : 최태성
담당 영역 : ingestion

vendored delta sync workflow 는 fake runner 로 대체하고, ChangedDocument→PageObject
매핑·raw_pages 적재·Chunking Queue 재투입·삭제 후보 집계를 검증한다. 코드 리뷰
재점검(A3·A13) 후속으로 acl_provider seam(주입 시 provider ACL, 미주입 시 space 합성)과
페이지 단위 격리(매핑 실패 1건 → failed_items 집계 + 나머지 계속)도 검증한다. 기존
reconcile_deletions 는 본 테스트에서 건드리지 않는다(무수정 보존).
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

from app.ingestion.sync import (
    DeltaSnapshotSeedRequest,
    DeltaSyncRequest,
    run_delta_sync,
    seed_delta_snapshot_from_current_metadata,
)
from app.ingestion.workers import QUEUE_CHUNKING
from app.ingestion.workers.publisher import FakeQueuePublisher
from app.storage.raw_store import FakeRawPageStore


def _changed(
    page_id: str,
    *,
    space_key: str = "ENG",
    version: int = 1,
    last_modified_at: str = "2026-05-14T01:00:00Z",
) -> SimpleNamespace:
    return SimpleNamespace(
        space={"space_id": "s1", "space_key": space_key, "space_name": "Engineering"},
        page={
            "page_id": page_id,
            "title": f"Title {page_id}",
            "page_url": f"/wiki/{page_id}",
            "last_modified_at": last_modified_at,
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


class _FakeMetadataClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_spaces(self) -> list[dict[str, Any]]:
        self.calls.append("list_spaces")
        return [{"id": "space-1", "key": "ENG", "name": "Engineering"}]

    def list_space_pages(self, space_id: str) -> list[dict[str, Any]]:
        self.calls.append(f"list_space_pages:{space_id}")
        return [
            {
                "id": "page-1",
                "title": "Runbook",
                "status": "current",
                "lastModifiedAt": "2026-05-14T01:00:00Z",
                "version": {"number": 3},
                "_links": {"webui": "/wiki/spaces/ENG/pages/page-1/Runbook"},
            }
        ]


def test_seed_delta_snapshot_from_current_metadata_writes_previous_snapshot(tmp_path) -> None:
    snapshot_path = tmp_path / "state" / "latest_snapshot.json"
    client = _FakeMetadataClient()
    request = DeltaSnapshotSeedRequest(
        previous_snapshot_path=str(snapshot_path),
        cloud_id="cloud-synthetic",
        access_token="token-synthetic",
        request_delay_seconds=0,
    )

    result = seed_delta_snapshot_from_current_metadata(
        request,
        client=client,
        minimum_collected_pages=1,
    )

    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    pages = payload["snapshot"]["pages"]
    assert result.seeded is True
    assert result.pages_seeded == 1
    assert client.calls == ["list_spaces", "list_space_pages:space-1"]
    assert payload["format_version"] == "data-sync-snapshot-v1"
    assert payload["snapshot"]["cloud_id"] == "cloud-synthetic"
    assert pages[0]["page_id"] == "page-1"
    assert pages[0]["version_number"] == 3
    assert pages[0]["last_modified_at"] == "2026-05-14T01:00:00Z"


def test_seed_delta_snapshot_skips_when_full_crawl_collected_fewer_pages(tmp_path) -> None:
    snapshot_path = tmp_path / "state" / "latest_snapshot.json"
    request = DeltaSnapshotSeedRequest(
        previous_snapshot_path=str(snapshot_path),
        cloud_id="cloud-synthetic",
        access_token="token-synthetic",
        request_delay_seconds=0,
    )

    result = seed_delta_snapshot_from_current_metadata(
        request,
        client=_FakeMetadataClient(),
        minimum_collected_pages=0,
    )

    assert result.seeded is False
    assert result.pages_seeded == 1
    assert result.skipped_reason == "metadata_pages_exceed_full_crawl_pages"
    assert not snapshot_path.exists()


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
    # provider 미주입 delta 는 빈 ACL(fail-closed) — 종전 space:ENG 합성은 2026-06-11
    # 회의 결정으로 제거(ACL 값의 space key 레거시 폐기). 색인 단계 INVALID_ACL 게이트
    # 가 제외하므로 운영 delta 는 admin-key provider 를 주입해야 한다.
    assert page.allowed_groups == []

    assert [m.routing_key for m in publisher.messages] == [QUEUE_CHUNKING]
    assert publisher.messages[0].body == {
        "page_id": "page-1",
        "space_key": "ENG",
        "version_number": 4,
        "source_type": "page",
    }


def test_run_delta_sync_passes_progress_callback_to_workflow_runner() -> None:
    events: list[dict[str, object]] = []

    def runner(
        *,
        config: Any,
        client: Any | None = None,
        snapshot_repository: Any | None = None,
        force_sequential: bool = False,
        progress_callback: Any | None = None,
    ) -> SimpleNamespace:
        assert progress_callback is not None
        progress_callback(
            {
                "phase": "changed_page_processed",
                "total_pages": 2,
                "processed_pages": 1,
                "failed_pages": 0,
            }
        )
        return SimpleNamespace(
            changed_documents=[],
            deleted_items=[],
            failed_items=[],
        )

    request = _request()
    request.progress_callback = events.append

    run_delta_sync(
        request,
        raw_store=FakeRawPageStore(),
        publisher=FakeQueuePublisher(),
        workflow_runner=runner,
    )

    assert events == [
        {
            "phase": "changed_page_processed",
            "total_pages": 2,
            "processed_pages": 1,
            "failed_pages": 0,
        }
    ]


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


def test_run_delta_sync_promotes_current_snapshot_to_previous_baseline(tmp_path) -> None:
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    previous_snapshot = tmp_path / "state" / "latest_snapshot.json"

    def runner(
        *,
        config: Any,
        client: Any | None = None,
        snapshot_repository: Any | None = None,
        force_sequential: bool = False,
    ) -> SimpleNamespace:
        current_snapshot = config.output_dir / "snapshots" / "latest_snapshot.json"
        current_snapshot.parent.mkdir(parents=True, exist_ok=True)
        current_snapshot.write_text('{"snapshot":"current"}\n', encoding="utf-8")
        return SimpleNamespace(
            changed_documents=[],
            deleted_items=[],
            failed_items=[],
            output_paths={"current_snapshot": str(current_snapshot)},
        )

    request = DeltaSyncRequest(
        previous_snapshot_path=str(previous_snapshot),
        cloud_id="cloud-synthetic",
        access_token="token-synthetic",
    )

    result = run_delta_sync(request, raw_store=store, publisher=publisher, workflow_runner=runner)

    assert result.changed_pages == 0
    assert previous_snapshot.read_text(encoding="utf-8") == '{"snapshot":"current"}\n'


def test_run_delta_sync_does_not_promote_incomplete_metadata_snapshot(tmp_path) -> None:
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    previous_snapshot = tmp_path / "state" / "latest_snapshot.json"

    def runner(
        *,
        config: Any,
        client: Any | None = None,
        snapshot_repository: Any | None = None,
        force_sequential: bool = False,
    ) -> SimpleNamespace:
        current_snapshot = config.output_dir / "snapshots" / "latest_snapshot.json"
        current_snapshot.parent.mkdir(parents=True, exist_ok=True)
        current_snapshot.write_text('{"snapshot":"partial"}\n', encoding="utf-8")
        return SimpleNamespace(
            changed_documents=[],
            deleted_items=[],
            failed_items=[
                SimpleNamespace(stage="fetch_page_metadata", retryable=True, item_id="space-1")
            ],
            output_paths={"current_snapshot": str(current_snapshot)},
        )

    request = DeltaSyncRequest(
        previous_snapshot_path=str(previous_snapshot),
        cloud_id="cloud-synthetic",
        access_token="token-synthetic",
    )

    result = run_delta_sync(request, raw_store=store, publisher=publisher, workflow_runner=runner)

    assert result.failed_items == 1
    assert not previous_snapshot.exists()


def test_run_delta_sync_promotes_snapshot_with_non_retryable_detail_failure(tmp_path) -> None:
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    previous_snapshot = tmp_path / "state" / "latest_snapshot.json"

    def runner(
        *,
        config: Any,
        client: Any | None = None,
        snapshot_repository: Any | None = None,
        force_sequential: bool = False,
    ) -> SimpleNamespace:
        current_snapshot = config.output_dir / "snapshots" / "latest_snapshot.json"
        current_snapshot.parent.mkdir(parents=True, exist_ok=True)
        current_snapshot.write_text('{"snapshot":"complete"}\n', encoding="utf-8")
        return SimpleNamespace(
            changed_documents=[],
            deleted_items=[],
            failed_items=[
                SimpleNamespace(
                    stage="fetch_page_detail",
                    retryable=False,
                    error_type="item_not_found",
                    item_id="archived-page",
                )
            ],
            output_paths={"current_snapshot": str(current_snapshot)},
        )

    request = DeltaSyncRequest(
        previous_snapshot_path=str(previous_snapshot),
        cloud_id="cloud-synthetic",
        access_token="token-synthetic",
    )

    result = run_delta_sync(request, raw_store=store, publisher=publisher, workflow_runner=runner)

    assert result.failed_items == 1
    assert previous_snapshot.read_text(encoding="utf-8") == '{"snapshot":"complete"}\n'


def test_run_delta_sync_passes_admin_key_config_to_agent() -> None:
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    captured: dict[str, Any] = {}

    def runner(
        *,
        config: Any,
        client: Any | None = None,
        snapshot_repository: Any | None = None,
        force_sequential: bool = False,
    ) -> SimpleNamespace:
        captured["config"] = config
        return SimpleNamespace(changed_documents=[], deleted_items=[], failed_items=[])

    request = DeltaSyncRequest(
        previous_snapshot_path="/tmp/previous_snapshot.json",
        cloud_id="cloud-synthetic",
        access_token="",
        use_admin_key=True,
        site_url="https://lina.atlassian.net",
        admin_email="admin@example.com",
        admin_api_token="admin-api-token",
    )

    result = run_delta_sync(
        request,
        raw_store=store,
        publisher=publisher,
        workflow_runner=runner,
    )

    config = captured["config"]
    assert result.failed_items == 0
    assert config.use_admin_key is True
    assert config.site_url == "https://lina.atlassian.net"
    assert config.admin_email == "admin@example.com"
    assert config.admin_api_token == "admin-api-token"
    assert config.access_token == ""


def test_run_delta_sync_logs_failed_item_details(caplog) -> None:
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    failed_item = SimpleNamespace(
        stage="LIST_SPACES",
        item_type="SYNC_JOB",
        item_id=None,
        error_type="auth_failure",
        retryable=False,
        error_message="Unauthorized; scope does not match",
    )
    runner = _fake_runner(changed=[], deleted=[], failed=[failed_item])

    with caplog.at_level(logging.WARNING):
        result = run_delta_sync(
            _request(),
            raw_store=store,
            publisher=publisher,
            workflow_runner=runner,
        )

    assert result.failed_items == 1
    assert "LIST_SPACES" in caplog.text
    assert "auth_failure" in caplog.text
    assert "scope does not match" in caplog.text


def test_run_delta_sync_uses_injected_acl_provider_over_space_synthesis() -> None:
    """A3 — acl_provider 주입 시 delta 재수집 ACL 이 provider 결과(restriction 기반)로 채워진다.

    미주입 기본은 space 합성(위 reingests 테스트) — 주입 시 합성으로 덮어쓰면 over/under-grant.
    """

    class _RecordingAclProvider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def get_page_acl(self, *, page_id: str, space_key: str) -> tuple[list[str], list[str]]:
            self.calls.append((page_id, space_key))
            return ["frontend-team"], ["712020:user-1"]

    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    provider = _RecordingAclProvider()
    runner = _fake_runner(changed=[_changed("page-1")], deleted=[], failed=[])

    result = run_delta_sync(
        _request(),
        raw_store=store,
        publisher=publisher,
        workflow_runner=runner,
        acl_provider=provider,
    )

    assert result.changed_pages == 1
    assert provider.calls == [("page-1", "ENG")]
    page = store.pages["page-1"]
    # 빈 ACL 폴백이 아니라 provider ACL 이 그대로 적재된다.
    assert page.allowed_groups == ["frontend-team"]
    assert page.allowed_users == ["712020:user-1"]


def test_run_delta_sync_isolates_single_mapping_failure_and_continues() -> None:
    """A13 — 매핑 실패(빈 last_modified) 1건은 failed_items 로 집계되고 나머지는 계속 처리된다.

    종전에는 ValueError 가 전파돼 delta 잡 전체가 FAILED 였다(페이지 단위 격리 회귀).
    """
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    runner = _fake_runner(
        changed=[
            _changed("page-bad", last_modified_at=""),  # 빈 last_modified → 매핑 ValueError
            _changed("page-ok", version=2),
        ],
        deleted=[],
        failed=[SimpleNamespace(item_id="page-x")],  # 워크플로 자체 실패 1건
    )

    result = run_delta_sync(
        _request(), raw_store=store, publisher=publisher, workflow_runner=runner
    )

    # 실패 1건이 잡을 죽이지 않고 정상 페이지는 적재·발행된다.
    assert result.changed_pages == 1
    assert set(store.pages) == {"page-ok"}
    assert [m.body["page_id"] for m in publisher.messages] == ["page-ok"]
    # failed_items = 워크플로 실패(1) + 페이지 격리 실패(1).
    assert result.failed_items == 2


def test_run_delta_sync_resets_provider_cache_at_run_start() -> None:
    """배포 전 점검(2026-06-10) — delta 런 시작 시 provider 캐시를 초기화한다.

    provider 는 잡 간 재사용되므로(full crawl fetch_pages 와 동일 규칙) reset 없이는
    직전 런이 캐시한 restriction 이 재사용돼 권한 변경이 반영되지 않는다.
    """

    class _ResetSpyAclProvider:
        def __init__(self) -> None:
            self.reset_calls = 0

        def reset_cache(self) -> None:
            self.reset_calls += 1

        def get_page_acl(self, *, page_id: str, space_key: str) -> tuple[list[str], list[str]]:
            return ["g"], []

    provider = _ResetSpyAclProvider()
    runner = _fake_runner(changed=[_changed("page-1")], deleted=[], failed=[])

    run_delta_sync(
        _request(),
        raw_store=FakeRawPageStore(),
        publisher=FakeQueuePublisher(),
        workflow_runner=runner,
        acl_provider=provider,
    )
    run_delta_sync(
        _request(),
        raw_store=FakeRawPageStore(),
        publisher=FakeQueuePublisher(),
        workflow_runner=runner,
        acl_provider=provider,
    )

    assert provider.reset_calls == 2


def test_run_delta_sync_normalizes_webui_link_when_site_url_set() -> None:
    """delta 경로도 §2-5 siteUrl 로 webui_link 를 absolute 정규화한다(full crawl 정합).

    backend-template 동기화(2026-06-11): site_url 미주입이면 종전대로 passthrough.
    """
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    runner = _fake_runner(changed=[_changed("page-1")], deleted=[], failed=[])

    run_delta_sync(
        _request(),
        raw_store=store,
        publisher=publisher,
        workflow_runner=runner,
        site_url="https://lina.atlassian.net",
    )

    assert store.pages["page-1"].webui_link == "https://lina.atlassian.net/wiki/page-1"


def test_run_delta_sync_keeps_relative_webui_link_without_site_url() -> None:
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    runner = _fake_runner(changed=[_changed("page-1")], deleted=[], failed=[])

    run_delta_sync(_request(), raw_store=store, publisher=publisher, workflow_runner=runner)

    assert store.pages["page-1"].webui_link == "/wiki/page-1"
