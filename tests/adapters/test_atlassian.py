"""AtlassianSourceAdapter 단위 테스트 — vendored Data Ingestion Agent 통합 경계 검증.

vendored ``run_full_crawl_workflow`` 를 fake Confluence client 와 함께 실제로 구동해
ProcessedDocument→PageObject 매핑·PoC ACL 합성·since 필터·list_active_ids 를 검증한다.
Admin Key 경로(read restriction → allowed_groups/allowed_users)와 빈 restriction 정책
(allow_authenticated / space_fallback / mark_missing)도 fake client 로 검증한다.
외부 HTTP 는 fake client 로 대체한다(루트 CLAUDE.md 테스트 규칙).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.adapters.atlassian import (
    AtlassianSourceAdapter,
    ConfluenceRestrictionAclProvider,
    parse_empty_restriction_policy,
    parse_group_identifier_fields,
    parse_read_restrictions_acl,
    synthesize_authenticated_acl,
)
from app.adapters.json_fixture import parse_atlassian_datetime


class _FakeConfluenceClient:
    """vendored DataIngestionClient protocol 을 만족하는 in-memory fake."""

    def __init__(
        self,
        *,
        spaces: list[dict[str, Any]],
        descendants_by_homepage: dict[str, list[dict[str, Any]]],
        details_by_page: dict[str, dict[str, Any]],
    ) -> None:
        self.spaces = spaces
        self.descendants_by_homepage = descendants_by_homepage
        self.details_by_page = details_by_page

    def list_spaces(self) -> list[dict[str, Any]]:
        return self.spaces

    def list_page_descendants(self, homepage_id: str) -> list[dict[str, Any]]:
        return self.descendants_by_homepage.get(homepage_id, [])

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
        descendants_by_homepage={"home-001": [_page_ref()]},
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


def test_fetch_pages_synthesizes_space_acl_and_empty_mvp_fields() -> None:
    page = next(iter(_adapter().fetch_pages()))

    # admin key off(=ACL provider 미주입): PoC space_key 합성으로 폴백한다.
    assert page.allowed_groups == ["space:ENG"]
    assert page.allowed_users == []
    assert page.is_acl_missing is False
    # 에이전트 MVP 미산출 필드는 빈 값으로 매핑된다.
    assert page.labels == []
    assert page.ancestors == []
    assert page.attachments == []


def test_fetch_pages_uses_injected_acl_provider() -> None:
    client = _FakeConfluenceClient(
        spaces=[_space()],
        descendants_by_homepage={"home-001": [_page_ref()]},
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
    assert parse_empty_restriction_policy("space_fallback") == "space_fallback"
    assert parse_empty_restriction_policy(" allow_authenticated ") == "allow_authenticated"


def test_parse_empty_restriction_policy_rejects_unknown_values() -> None:
    try:
        parse_empty_restriction_policy("public")
    except ValueError as exc:
        assert "atlassian_empty_restriction_policy" in str(exc)
        assert "mark_missing" in str(exc)
        assert "space_fallback" in str(exc)
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


def test_confluence_restriction_acl_provider_can_fallback_to_space_acl() -> None:
    client = _FakeRestrictionClient(
        {"operation": "read", "restrictions": {"group": {"results": []}, "user": {"results": []}}}
    )
    provider = ConfluenceRestrictionAclProvider(
        client=client, empty_restriction_policy="space_fallback"
    )

    groups, users = provider.get_page_acl(page_id="page-001", space_key="ENG")

    assert groups == ["space:ENG"]
    assert users == []


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
