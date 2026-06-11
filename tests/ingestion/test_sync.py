"""run_delta_sync 단위 테스트 — vendored Data Sync Agent 통합 경계 검증.

vendored delta sync workflow 는 fake runner 로 대체하고, ChangedDocument→PageObject
매핑·raw_pages 적재·Chunking Queue 재투입·삭제 후보 집계를 검증한다. 코드 리뷰
재점검(A3·A13) 후속으로 acl_provider seam(주입 시 provider ACL, 미주입 시 space 합성)과
페이지 단위 격리(매핑 실패 1건 → failed_items 집계 + 나머지 계속)도 검증한다. 기존
reconcile_deletions 는 본 테스트에서 건드리지 않는다(무수정 보존).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.ingestion.sync import DeltaSyncRequest, run_delta_sync
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
