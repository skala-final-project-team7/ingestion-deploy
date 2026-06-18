"""AtlassianSourceAdapter 단위 테스트 — vendored Data Ingestion Agent 통합 경계 검증.

작성자 : 최태성
담당 영역 : ingestion

vendored ``run_full_crawl_workflow`` 를 fake Confluence client 와 함께 실제로 구동해
ProcessedDocument→PageObject 매핑·provider 부재 시 빈 ACL(fail-closed)·since 필터·
list_active_ids 를 검증한다. Admin Key 경로(read restriction → allowed_groups/allowed_users)
와 빈 restriction 정책(allow_authenticated / mark_missing — 기본은 fail-closed mark_missing,
코드 리뷰 A2; space_fallback 은 2026-06-11 제거되어 거부를 검증)도 fake client 로 검증한다.
``build_restriction_acl_provider``(full crawl/
delta 공용 seam — 코드 리뷰 A3)의 Settings 기반 분기도 검증한다.
외부 HTTP 는 fake client 로 대체한다(루트 CLAUDE.md 테스트 규칙).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import SecretStr

from app.adapters.atlassian import (
    AtlassianSourceAdapter,
    ConfluenceRestrictionAclProvider,
    build_restriction_acl_provider,
    normalize_webui_link,
    parse_empty_restriction_policy,
    parse_group_identifier_fields,
    parse_read_restrictions_acl,
    synthesize_authenticated_acl,
)
from app.adapters.json_fixture import parse_atlassian_datetime
from app.config import Settings


class _FakeConfluenceClient:
    """vendored DataIngestionClient protocol 을 만족하는 in-memory fake."""

    def __init__(
        self,
        *,
        spaces: list[dict[str, Any]],
        pages_by_space: dict[str, list[dict[str, Any]]],
        details_by_page: dict[str, dict[str, Any]],
    ) -> None:
        self.spaces = spaces
        self.pages_by_space = pages_by_space
        self.details_by_page = details_by_page

    def list_spaces(self) -> list[dict[str, Any]]:
        return self.spaces

    def list_page_descendants(self, homepage_id: str) -> list[dict[str, Any]]:
        for space in self.spaces:
            if space.get("homepageId") == homepage_id:
                return self.pages_by_space.get(str(space.get("id") or ""), [])
        return []

    def list_space_pages(self, space_id: str) -> list[dict[str, Any]]:
        return self.pages_by_space.get(space_id, [])

    def get_page_detail(self, page_id: str) -> dict[str, Any]:
        return self.details_by_page[page_id]


class _FakeAclProvider:
    def get_page_acl(self, *, page_id: str, space_key: str) -> tuple[list[str], list[str]]:
        assert page_id == "page-001"
        assert space_key == "ENG"
        return ["frontend-team"], ["712020:user-1"]


class _FakeRestrictionClient:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.requested_page_ids: list[str] = []

    def get_page_read_restrictions(self, page_id: str) -> dict[str, Any]:
        self.requested_page_ids.append(page_id)
        return self.raw


def _space() -> dict[str, Any]:
    return {"id": "space-001", "key": "ENG", "name": "Engineering", "homepageId": "home-001"}


def _page_ref() -> dict[str, Any]:
    return {"id": "page-001", "parentId": "parent-001", "depth": 1, "position": 0}


def _page_detail() -> dict[str, Any]:
    return {
        "id": "page-001",
        "title": "Runbook",
        "status": "current",
        "body": {"storage": {"value": "<h1>Runbook</h1><p>Restart</p>"}},
        "createdAt": "2026-05-14T00:00:00Z",
        "version": {"number": 3, "createdAt": "2026-05-14T01:00:00Z"},
        "_links": {"webui": "/wiki/spaces/ENG/pages/page-001/Runbook"},
    }


def _adapter() -> AtlassianSourceAdapter:
    client = _FakeConfluenceClient(
        spaces=[_space()],
        pages_by_space={"space-001": [_page_ref()]},
        details_by_page={"page-001": _page_detail()},
    )
    return AtlassianSourceAdapter(
        cloud_id="cloud-synthetic",
        access_token="token-synthetic",
        client=client,
        request_delay_seconds=0,
    )


def test_fetch_pages_maps_processed_document_to_page_object() -> None:
    pages = list(_adapter().fetch_pages())

    assert len(pages) == 1
    page = pages[0]
    assert page.page_id == "page-001"
    assert page.space_key == "ENG"
    assert page.title == "Runbook"
    assert "<h1>Runbook</h1>" in page.body_html
    assert page.version_number == 3
    assert page.webui_link == "/wiki/spaces/ENG/pages/page-001/Runbook"
    assert page.last_modified == parse_atlassian_datetime("2026-05-14T01:00:00Z")


def test_fetch_pages_without_provider_yields_empty_acl_and_empty_mvp_fields() -> None:
    page = next(iter(_adapter().fetch_pages()))

    # admin key off(=ACL provider 미주입): 빈 ACL(fail-closed) — 색인 단계 INVALID_ACL
    # 게이트가 제외한다. 종전 PoC space_key 합성은 2026-06-11 회의 결정으로 제거됐다
    # (ACL 값의 space key 레거시 폐기 — ADR 0002 superseded).
    assert page.allowed_groups == []
    assert page.allowed_users == []
    assert page.is_acl_missing is True
    # 에이전트 MVP 미산출 필드는 빈 값으로 매핑된다.
    assert page.labels == []
    assert page.ancestors == []
    assert page.attachments == []


def test_fetch_pages_uses_injected_acl_provider() -> None:
    client = _FakeConfluenceClient(
        spaces=[_space()],
        pages_by_space={"space-001": [_page_ref()]},
        details_by_page={"page-001": _page_detail()},
    )
    adapter = AtlassianSourceAdapter(
        cloud_id="cloud-synthetic",
        access_token="token-synthetic",
        client=client,
        acl_provider=_FakeAclProvider(),
        request_delay_seconds=0,
    )

    page = next(iter(adapter.fetch_pages()))

    assert page.allowed_groups == ["frontend-team"]
    assert page.allowed_users == ["712020:user-1"]


def test_fetch_pages_since_filter_excludes_older_pages() -> None:
    future = datetime.fromisoformat("2030-01-01T00:00:00+00:00")
    assert list(_adapter().fetch_pages(since=future)) == []


def test_list_active_ids_returns_page_ids_without_attachments() -> None:
    ids = _adapter().list_active_ids()

    assert ids.pages == {"page-001"}
    assert ids.attachments == set()


def test_watch_changes_is_empty_stream() -> None:
    assert list(_adapter().watch_changes()) == []


def test_parse_read_restrictions_acl_maps_groups_and_users() -> None:
    raw = {
        "operation": "read",
        "restrictions": {
            "group": {
                "results": [
                    {"id": "group-id-1", "name": "frontend"},
                    {"name": "fallback-name"},
                    {"id": "group-id-1", "name": "duplicated"},
                ]
            },
            "user": {
                "results": [
                    {"accountId": "712020:user-1", "displayName": "신유진"},
                    {"accountId": "712020:user-2", "displayName": "sunny"},
                    {"accountId": "712020:user-1", "displayName": "duplicate"},
                ]
            },
        },
    }

    groups, users = parse_read_restrictions_acl(raw)

    assert groups == ["group-id-1", "fallback-name"]
    assert users == ["712020:user-1", "712020:user-2"]


def test_parse_read_restrictions_acl_uses_configured_group_field_order() -> None:
    raw = {
        "operation": "read",
        "restrictions": {
            "group": {
                "results": [
                    {"id": "group-id-1", "name": "frontend"},
                    {"id": "group-id-2", "name": "platform"},
                ]
            },
            "user": {"results": []},
        },
    }

    groups, users = parse_read_restrictions_acl(raw, group_identifier_fields=("name", "id"))

    assert groups == ["frontend", "platform"]
    assert users == []


def test_parse_read_restrictions_acl_applies_group_prefix() -> None:
    raw = {
        "operation": "read",
        "restrictions": {
            "group": {"results": [{"id": "group-id-1"}, {"id": "group-id-1"}]},
            "user": {"results": []},
        },
    }

    groups, _users = parse_read_restrictions_acl(raw, group_acl_prefix="confluence-group:")

    assert groups == ["confluence-group:group-id-1"]


def test_parse_group_identifier_fields_splits_comma_separated_values() -> None:
    assert parse_group_identifier_fields(" name, id ,groupId ") == ("name", "id", "groupId")


def test_parse_group_identifier_fields_rejects_empty_values() -> None:
    try:
        parse_group_identifier_fields(" , ")
    except ValueError as exc:
        assert "atlassian_group_acl_field_order" in str(exc)
    else:
        raise AssertionError("empty group field order must be rejected")


def test_parse_empty_restriction_policy_accepts_known_values() -> None:
    assert parse_empty_restriction_policy(" mark_missing ") == "mark_missing"
    assert parse_empty_restriction_policy(" allow_authenticated ") == "allow_authenticated"


def test_parse_empty_restriction_policy_rejects_unknown_values() -> None:
    # space_fallback 도 2026-06-11 제거 후 미지원 값이다(ACL 의 space key 레거시 폐기).
    for invalid in ("public", "space_fallback"):
        try:
            parse_empty_restriction_policy(invalid)
        except ValueError as exc:
            assert "atlassian_empty_restriction_policy" in str(exc)
            assert "mark_missing" in str(exc)
            assert "allow_authenticated" in str(exc)
        else:
            raise AssertionError("unknown empty restriction policy must be rejected")


def test_synthesize_authenticated_acl_returns_sentinel_group() -> None:
    groups, users = synthesize_authenticated_acl("*")

    assert groups == ["*"]
    assert users == []


def test_synthesize_authenticated_acl_rejects_empty_token() -> None:
    try:
        synthesize_authenticated_acl("   ")
    except ValueError as exc:
        assert "atlassian_public_acl_group" in str(exc)
    else:
        raise AssertionError("empty public acl group token must be rejected")


def test_confluence_restriction_acl_provider_marks_empty_restriction_missing() -> None:
    client = _FakeRestrictionClient(
        {"operation": "read", "restrictions": {"group": {"results": []}, "user": {"results": []}}}
    )
    provider = ConfluenceRestrictionAclProvider(client=client)

    groups, users = provider.get_page_acl(page_id="page-001", space_key="ENG")

    assert groups == []
    assert users == []
    assert client.requested_page_ids == ["page-001"]


def test_confluence_restriction_acl_provider_rejects_removed_space_fallback() -> None:
    # 2026-06-11 — space_fallback 정책은 제거됐다(ACL 값의 space key 레거시 폐기).
    # provider 생성 시점에 명확히 거부되어야 한다(무음 동작 변경 방지).
    client = _FakeRestrictionClient(
        {"operation": "read", "restrictions": {"group": {"results": []}, "user": {"results": []}}}
    )
    try:
        ConfluenceRestrictionAclProvider(client=client, empty_restriction_policy="space_fallback")
    except ValueError as exc:
        assert "space_fallback" in str(exc) or "atlassian_empty_restriction_policy" in str(exc)
    else:
        raise AssertionError("removed policy space_fallback must be rejected")


def test_confluence_restriction_acl_provider_allows_authenticated_on_empty() -> None:
    client = _FakeRestrictionClient(
        {"operation": "read", "restrictions": {"group": {"results": []}, "user": {"results": []}}}
    )
    provider = ConfluenceRestrictionAclProvider(
        client=client,
        empty_restriction_policy="allow_authenticated",
        public_acl_group="*",
    )

    groups, users = provider.get_page_acl(page_id="page-001", space_key="ENG")

    # 제한 없는 페이지 → sentinel group 부여(모든 인증 사용자 허용), is_acl_missing 회피.
    assert groups == ["*"]
    assert users == []


def test_confluence_restriction_acl_provider_allow_authenticated_honors_custom_token() -> None:
    client = _FakeRestrictionClient(
        {"operation": "read", "restrictions": {"group": {"results": []}, "user": {"results": []}}}
    )
    provider = ConfluenceRestrictionAclProvider(
        client=client,
        empty_restriction_policy="allow_authenticated",
        public_acl_group="public",
    )

    groups, users = provider.get_page_acl(page_id="page-001", space_key="ENG")

    assert groups == ["public"]
    assert users == []


def test_confluence_restriction_acl_provider_keeps_explicit_restrictions_over_policy() -> None:
    # 명시 restriction 이 있으면 빈-정책(allow_authenticated)을 적용하지 않고 실제 ACL 을 쓴다.
    client = _FakeRestrictionClient(
        {
            "operation": "read",
            "restrictions": {
                "group": {"results": [{"id": "frontend"}]},
                "user": {"results": [{"accountId": "712020:user-1"}]},
            },
        }
    )
    provider = ConfluenceRestrictionAclProvider(
        client=client, empty_restriction_policy="allow_authenticated"
    )

    groups, users = provider.get_page_acl(page_id="page-001", space_key="ENG")

    assert groups == ["frontend"]
    assert users == ["712020:user-1"]


def test_confluence_restriction_acl_provider_rejects_unknown_empty_policy() -> None:
    client = _FakeRestrictionClient({"operation": "read", "restrictions": {}})

    try:
        ConfluenceRestrictionAclProvider(client=client, empty_restriction_policy="public")
    except ValueError as exc:
        assert "atlassian_empty_restriction_policy" in str(exc)
    else:
        raise AssertionError("unknown empty restriction policy must be rejected")


def test_confluence_restriction_acl_provider_passes_group_mapping_options() -> None:
    client = _FakeRestrictionClient(
        {
            "operation": "read",
            "restrictions": {
                "group": {"results": [{"id": "group-id-1", "name": "frontend"}]},
                "user": {"results": []},
            },
        }
    )
    provider = ConfluenceRestrictionAclProvider(
        client=client,
        group_identifier_fields=("name", "id"),
        group_acl_prefix="confluence-group:",
    )

    groups, users = provider.get_page_acl(page_id="page-001", space_key="ENG")

    assert groups == ["confluence-group:frontend"]
    assert users == []


# --- build_restriction_acl_provider (코드 리뷰 A3 — full crawl/delta 공용 seam) ---


def test_settings_default_empty_restriction_policy_is_mark_missing() -> None:
    """A2 — 기본 정책은 fail-closed mark_missing 이다(상속 제한 문서 과다 노출 방지).

    allow_authenticated 는 ancestor restriction 조회 구현 전까지 opt-in 전용이며,
    provider 무인자 기본값도 Settings 기본값과 동일해야 한다(이중 계약 고정).
    """
    assert Settings(_env_file=None).atlassian_empty_restriction_policy == "mark_missing"
    provider = ConfluenceRestrictionAclProvider(client=object())
    assert provider.empty_restriction_policy == "mark_missing"


def test_build_restriction_acl_provider_none_when_admin_key_disabled() -> None:
    """admin key off(기본) → None — 호출자(from_settings/build_delta_runner)가 space 합성 폴백."""
    assert build_restriction_acl_provider(Settings(atlassian_use_admin_key=False)) is None


def test_build_restriction_acl_provider_builds_provider_with_settings_policy() -> None:
    """admin key on → Settings 의 정책/그룹 매핑 옵션이 반영된 provider 를 만든다.

    2026-06-11 — admin-key 경로는 admin API Token(Basic)+site URL 이 필수다(v2.6.1).
    """
    settings = Settings(
        atlassian_use_admin_key=True,
        atlassian_cloud_id="cloud-synthetic",
        atlassian_site_url="https://lina.atlassian.net",
        atlassian_admin_email="admin@lina.example",
        atlassian_admin_api_token=SecretStr("api-token-synthetic"),
        atlassian_group_acl_field_order="name,id",
        atlassian_group_acl_prefix="confluence-group:",
        atlassian_public_acl_group="*",
        atlassian_empty_restriction_policy="mark_missing",
    )

    provider = build_restriction_acl_provider(settings)

    assert isinstance(provider, ConfluenceRestrictionAclProvider)
    # Settings 기본 정책(mark_missing — A2 fail-closed)이 그대로 흐른다.
    assert provider.empty_restriction_policy == "mark_missing"
    assert provider.group_identifier_fields == ("name", "id")
    assert provider.group_acl_prefix == "confluence-group:"
    assert provider.public_acl_group == "*"


def test_build_restriction_acl_provider_honors_opt_in_policy() -> None:
    """opt-in 정책(allow_authenticated)을 명시하면 provider 에 그대로 반영된다."""
    settings = Settings(
        atlassian_use_admin_key=True,
        atlassian_cloud_id="cloud-synthetic",
        atlassian_site_url="https://lina.atlassian.net",
        atlassian_admin_email="admin@lina.example",
        atlassian_admin_api_token=SecretStr("api-token-synthetic"),
        atlassian_empty_restriction_policy="allow_authenticated",
    )

    provider = build_restriction_acl_provider(settings)

    assert isinstance(provider, ConfluenceRestrictionAclProvider)
    assert provider.empty_restriction_policy == "allow_authenticated"


# --- 2026-06-10(A2 후속) — ancestor restriction 상속 워크 회귀 ---


def _restricted_raw(group: str) -> dict[str, Any]:
    return {
        "operation": "read",
        "restrictions": {
            "group": {"results": [{"id": group}]},
            "user": {"results": []},
        },
    }


_EMPTY_RAW: dict[str, Any] = {
    "operation": "read",
    "restrictions": {"group": {"results": []}, "user": {"results": []}},
}


class _FakeRestrictionWalkClient:
    """페이지별 restriction + parentId 체인을 가진 fake — 호출 횟수를 추적한다."""

    def __init__(
        self,
        *,
        restrictions: dict[str, dict[str, Any]],
        parents: dict[str, str | None] | None = None,
    ) -> None:
        self.restrictions = restrictions
        self.parents = parents or {}
        self.restriction_calls: list[str] = []
        self.detail_calls: list[str] = []

    def get_page_read_restrictions(self, page_id: str) -> dict[str, Any]:
        self.restriction_calls.append(page_id)
        return self.restrictions.get(page_id, _EMPTY_RAW)

    def get_page_detail(self, page_id: str) -> dict[str, Any]:
        self.detail_calls.append(page_id)
        return {"id": page_id, "parentId": self.parents.get(page_id)}


def test_ancestor_walk_inherits_nearest_restricted_ancestor() -> None:
    """빈 page restriction 은 가까운 제한 조상의 ACL 을 상속한다(전달된 체인 사용)."""
    client = _FakeRestrictionWalkClient(
        restrictions={
            "anc-near": _restricted_raw("near-team"),
            "anc-far": _restricted_raw("far-team"),
        }
    )
    provider = ConfluenceRestrictionAclProvider(client=client)

    groups, users = provider.get_page_acl(
        page_id="page-1", space_key="ENG", ancestor_ids=["anc-empty", "anc-near", "anc-far"]
    )

    assert groups == ["near-team"]
    assert users == []
    # 전달된 체인을 썼으므로 parentId API(get_page_detail)는 호출되지 않는다.
    assert client.detail_calls == []
    # near 에서 멈춰 far 는 조회하지 않는다(가까운 조상 우선 + 조기 종료).
    assert client.restriction_calls == ["page-1", "anc-empty", "anc-near"]


def test_ancestor_walk_all_empty_falls_back_to_policy() -> None:
    """본인+조상 전부 빈 restriction 이면 정책 폴백(mark_missing → 빈 ACL)."""
    client = _FakeRestrictionWalkClient(restrictions={})
    provider = ConfluenceRestrictionAclProvider(client=client)

    groups, users = provider.get_page_acl(
        page_id="page-1", space_key="ENG", ancestor_ids=["anc-1", "anc-2"]
    )

    assert (groups, users) == ([], [])


def test_ancestor_walk_via_page_detail_when_chain_not_provided() -> None:
    """ancestor_ids 미전달(delta 경로)이면 get_page_detail parentId 를 따라 걷는다."""
    client = _FakeRestrictionWalkClient(
        restrictions={"anc-2": _restricted_raw("ops-team")},
        parents={"page-1": "anc-1", "anc-1": "anc-2", "anc-2": None},
    )
    provider = ConfluenceRestrictionAclProvider(client=client)

    groups, _users = provider.get_page_acl(page_id="page-1", space_key="ENG")

    assert groups == ["ops-team"]
    assert client.detail_calls == ["page-1", "anc-1"]


def test_ancestor_walk_caches_shared_ancestor_restrictions() -> None:
    """형제 페이지들이 같은 조상을 공유하면 restriction 조회는 조상당 1회다(캐시)."""
    client = _FakeRestrictionWalkClient(restrictions={"anc-1": _restricted_raw("team-a")})
    provider = ConfluenceRestrictionAclProvider(client=client)

    for page_id in ("page-1", "page-2", "page-3"):
        groups, _ = provider.get_page_acl(page_id=page_id, space_key="ENG", ancestor_ids=["anc-1"])
        assert groups == ["team-a"]

    assert client.restriction_calls.count("anc-1") == 1


def test_ancestor_walk_disabled_restores_legacy_policy_fallback() -> None:
    """ancestor_lookup_enabled=False 면 조상이 제한돼 있어도 정책 폴백 직행(종전 동작)."""
    client = _FakeRestrictionWalkClient(restrictions={"anc-1": _restricted_raw("team-a")})
    provider = ConfluenceRestrictionAclProvider(client=client, ancestor_lookup_enabled=False)

    groups, users = provider.get_page_acl(page_id="page-1", space_key="ENG", ancestor_ids=["anc-1"])

    assert (groups, users) == ([], [])
    assert "anc-1" not in client.restriction_calls


def test_ancestor_walk_detail_failure_degrades_to_policy() -> None:
    """parentId 조회 실패는 수집을 깨지 않고 정책 폴백으로 진행한다."""

    class _BrokenDetailClient(_FakeRestrictionWalkClient):
        def get_page_detail(self, page_id: str) -> dict[str, Any]:
            raise RuntimeError("boom")

    provider = ConfluenceRestrictionAclProvider(client=_BrokenDetailClient(restrictions={}))

    groups, users = provider.get_page_acl(page_id="page-1", space_key="ENG")

    assert (groups, users) == ([], [])


def test_adapter_passes_crawl_ancestor_chain_to_capable_provider() -> None:
    """어댑터는 크롤 parent_id 체인을 capability provider 에 ancestor_ids 로 전달한다."""

    class _CapturingProvider:
        supports_ancestor_ids = True

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def get_page_acl(
            self,
            *,
            page_id: str,
            space_key: str,
            ancestor_ids: Any = None,
        ) -> tuple[list[str], list[str]]:
            self.calls.append(
                {"page_id": page_id, "space_key": space_key, "ancestor_ids": ancestor_ids}
            )
            return ["g"], []

    provider = _CapturingProvider()
    client = _FakeConfluenceClient(
        spaces=[_space()],
        pages_by_space={"space-001": [_page_ref()]},
        details_by_page={"page-001": _page_detail()},
    )
    adapter = AtlassianSourceAdapter(
        cloud_id="cloud-synthetic",
        access_token="token-synthetic",
        client=client,
        acl_provider=provider,  # type: ignore[arg-type]
        request_delay_seconds=0,
    )

    pages = list(adapter.fetch_pages())

    assert pages[0].allowed_groups == ["g"]
    assert provider.calls[0]["page_id"] == "page-001"
    # 단일 문서 크롤 — parent(parent-001)는 문서 목록 밖이라도 ACL 워크용 체인에 포함된다.
    assert provider.calls[0]["ancestor_ids"] == ["parent-001"]


def test_adapter_maps_space_id_and_name_to_page_object() -> None:
    """A8 잔여 — SpaceInfo(id/name)가 PageObject.space_id/space_name 으로 흐른다."""
    page = next(iter(_adapter().fetch_pages()))

    assert page.space_id == "space-001"
    assert page.space_name == "Engineering"


# --- 배포 전 점검(2026-06-10) — ACL 캐시 런 단위 초기화 + restriction 실패 페이지 격리 ---


def test_reset_cache_refetches_restrictions_after_change() -> None:
    """reset_cache() 후에는 변경된 restriction 이 다시 조회된다(런 간 캐시 잔존 방지).

    provider 는 startup 1회 생성되어 잡 간 재사용되므로, 캐시를 런 단위로 비우지
    않으면 Confluence 권한 변경이 재수집에 반영되지 않는다(over/under-grant).
    """
    client = _FakeRestrictionWalkClient(restrictions={"page-1": _restricted_raw("old-team")})
    provider = ConfluenceRestrictionAclProvider(client=client)

    groups, _ = provider.get_page_acl(page_id="page-1", space_key="ENG")
    assert groups == ["old-team"]

    # 권한 변경 후 캐시 잔존 — 같은 런 안에서는 메모이즈가 정상이다.
    client.restrictions["page-1"] = _restricted_raw("new-team")
    groups, _ = provider.get_page_acl(page_id="page-1", space_key="ENG")
    assert groups == ["old-team"]

    # 런 시작(reset_cache) 후에는 새 restriction 을 본다.
    provider.reset_cache()
    groups, _ = provider.get_page_acl(page_id="page-1", space_key="ENG")
    assert groups == ["new-team"]


def test_fetch_pages_resets_provider_cache_per_run() -> None:
    """fetch_pages 는 런 시작 시 provider.reset_cache() 를 호출한다(잡 간 재사용 대비)."""

    class _ResetSpyProvider:
        supports_ancestor_ids = True

        def __init__(self) -> None:
            self.reset_calls = 0

        def reset_cache(self) -> None:
            self.reset_calls += 1

        def get_page_acl(
            self,
            *,
            page_id: str,
            space_key: str,
            ancestor_ids: Any = None,
        ) -> tuple[list[str], list[str]]:
            return ["g"], []

    provider = _ResetSpyProvider()
    client = _FakeConfluenceClient(
        spaces=[_space()],
        pages_by_space={"space-001": [_page_ref()]},
        details_by_page={"page-001": _page_detail()},
    )
    adapter = AtlassianSourceAdapter(
        cloud_id="cloud-synthetic",
        access_token="token-synthetic",
        client=client,
        acl_provider=provider,  # type: ignore[arg-type]
        request_delay_seconds=0,
    )

    list(adapter.fetch_pages())
    list(adapter.fetch_pages())

    assert provider.reset_calls == 2


def test_resolve_acl_provider_failure_degrades_fail_closed() -> None:
    """restriction 조회 하드 실패는 페이지 단위로 빈 ACL(fail-closed) 강등된다.

    1건의 API 실패가 full crawl 잡 전체를 FAILED 로 만들지 않고(delta A13 정합),
    빈 ACL 페이지는 chunking 의 INVALID_ACL 게이트가 색인에서 제외한다.
    """

    class _FailingProvider:
        supports_ancestor_ids = True

        def get_page_acl(
            self,
            *,
            page_id: str,
            space_key: str,
            ancestor_ids: Any = None,
        ) -> tuple[list[str], list[str]]:
            raise RuntimeError("restriction API 5xx — 재시도 소진")

    client = _FakeConfluenceClient(
        spaces=[_space()],
        pages_by_space={"space-001": [_page_ref()]},
        details_by_page={"page-001": _page_detail()},
    )
    adapter = AtlassianSourceAdapter(
        cloud_id="cloud-synthetic",
        access_token="token-synthetic",
        client=client,
        acl_provider=_FailingProvider(),  # type: ignore[arg-type]
        request_delay_seconds=0,
    )

    pages = list(adapter.fetch_pages())

    assert len(pages) == 1
    assert (pages[0].allowed_groups, pages[0].allowed_users) == ([], [])
    assert pages[0].is_acl_missing


# --- webui_link absolute 정규화 (backend-template 동기화 2026-06-11 — api-spec §2-5 siteUrl) ---


def test_normalize_webui_link_prefixes_site_url_for_wiki_paths() -> None:
    # v1 형(/wiki/ 포함) 상대경로 — site_url 만 앞에 붙는다.
    assert normalize_webui_link("/wiki/spaces/ENG/pages/1/T", "https://lina.atlassian.net") == (
        "https://lina.atlassian.net/wiki/spaces/ENG/pages/1/T"
    )
    # trailing slash 는 정규화한다.
    assert normalize_webui_link("/wiki/x", "https://lina.atlassian.net/") == (
        "https://lina.atlassian.net/wiki/x"
    )


def test_normalize_webui_link_adds_wiki_base_for_v2_relative_paths() -> None:
    # v2 API `_links.webui` 는 /wiki base 상대경로(/spaces/...)로 온다.
    assert normalize_webui_link("/spaces/ENG/pages/1/T", "https://lina.atlassian.net") == (
        "https://lina.atlassian.net/wiki/spaces/ENG/pages/1/T"
    )


def test_normalize_webui_link_keeps_absolute_and_empty_inputs() -> None:
    absolute = "https://lina.atlassian.net/wiki/spaces/ENG/pages/1/T"
    assert normalize_webui_link(absolute, "https://other.atlassian.net") == absolute
    # 빈 site_url(미주입 환경)·빈 webui 는 passthrough — 조립부 WARNING 으로 안내.
    assert normalize_webui_link("/wiki/x", "") == "/wiki/x"
    assert normalize_webui_link("", "https://lina.atlassian.net") == ""


def test_fetch_pages_normalizes_webui_link_when_site_url_set() -> None:
    """site_url 주입 시 Qdrant webui_link/RAG sources[].url 이 absolute 로 적재된다(§2-5)."""
    client = _FakeConfluenceClient(
        spaces=[_space()],
        pages_by_space={"space-001": [_page_ref()]},
        details_by_page={"page-001": _page_detail()},
    )
    adapter = AtlassianSourceAdapter(
        cloud_id="cloud-synthetic",
        access_token="token-synthetic",
        client=client,
        request_delay_seconds=0,
        site_url="https://lina.atlassian.net",
    )

    page = next(iter(adapter.fetch_pages()))

    assert page.webui_link == "https://lina.atlassian.net/wiki/spaces/ENG/pages/page-001/Runbook"


def test_from_settings_passes_site_url_for_webui_normalization() -> None:
    settings = Settings(
        source_type="atlassian",
        atlassian_cloud_id="cloud-1",
        atlassian_access_token=SecretStr("token-1"),
        atlassian_site_url="https://lina.atlassian.net",
    )

    adapter = AtlassianSourceAdapter.from_settings(settings)

    assert adapter._site_url == "https://lina.atlassian.net"  # noqa: SLF001 — 배선 검증
