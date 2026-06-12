"""Confluence Trash 소스 단위 테스트 (featureI-5b).

작성자 : 최태성
담당 영역 : ingestion

parse_trashed_content(분류·graceful skip), ConfluenceTrashSource(_links.next 페이지네이션·
누적·빈 space), ConfluenceTrashContentClient(URL 구성·인증 헤더)를 fake 로 검증한다.
네트워크 호출은 없다(transport/client 주입).
"""

from __future__ import annotations

from typing import Any

from app.adapters.confluence_trash import (
    ConfluenceTrashContentClient,
    ConfluenceTrashSource,
    TrashedIds,
    parse_trashed_content,
)


def test_parse_trashed_content_classifies_page_and_attachment() -> None:
    results = [
        {"id": "100", "type": "page", "status": "trashed"},
        {"id": "200", "type": "attachment", "status": "trashed"},
        {"id": "300", "type": "page"},  # status 생략 — trashed 로 간주
    ]

    parsed = parse_trashed_content(results)

    assert parsed.pages == {"100", "300"}
    assert parsed.attachments == {"200"}
    assert parsed.is_empty is False


def test_parse_trashed_content_skips_missing_id_and_non_trashed() -> None:
    results: list[Any] = [
        {"type": "page", "status": "trashed"},  # id 없음 → skip
        {"id": "  ", "type": "page"},  # 공백 id → skip
        {"id": "400", "type": "page", "status": "current"},  # trashed 아님 → skip
        "not-a-dict",  # 비-dict → skip
        {"id": "500", "type": "page", "status": "trashed"},
    ]

    parsed = parse_trashed_content(results)

    assert parsed.pages == {"500"}
    assert parsed.attachments == set()


def test_parse_trashed_content_empty_is_empty() -> None:
    assert parse_trashed_content([]).is_empty is True


class _FakeTrashContentClient:
    """cursor 기반 2페이지 응답을 돌려주는 fake — _links.next 페이지네이션 검증용."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def fetch_trashed(self, *, space_key: str, cursor: str | None) -> dict[str, Any]:
        self.calls.append((space_key, cursor))
        if cursor is None:
            return {
                "results": [{"id": "100", "type": "page", "status": "trashed"}],
                "_links": {"next": "/wiki/rest/api/content?status=trashed&start=1"},
            }
        return {
            "results": [{"id": "200", "type": "attachment", "status": "trashed"}],
            "_links": {},  # next 없음 → 종료
        }


def test_confluence_trash_source_paginates_and_accumulates() -> None:
    client = _FakeTrashContentClient()
    source = ConfluenceTrashSource(client=client, space_keys=["CLOUD"])

    trashed = source.list_trashed_ids()

    assert trashed.pages == {"100"}
    assert trashed.attachments == {"200"}
    # 2페이지 순회: 첫 호출 cursor=None, 둘째 호출 cursor=_links.next.
    assert client.calls == [
        ("CLOUD", None),
        ("CLOUD", "/wiki/rest/api/content?status=trashed&start=1"),
    ]


def test_confluence_trash_source_empty_space_keys_returns_empty() -> None:
    client = _FakeTrashContentClient()
    source = ConfluenceTrashSource(client=client, space_keys=[])

    trashed = source.list_trashed_ids()

    assert trashed == TrashedIds()
    assert client.calls == []


def test_confluence_trash_source_respects_max_pages_guard() -> None:
    # 항상 next 를 주는 client → max_pages 상한에서 멈춰야 한다(무한 루프 방지).
    class _AlwaysNext:
        def __init__(self) -> None:
            self.n = 0

        def fetch_trashed(self, *, space_key: str, cursor: str | None) -> dict[str, Any]:
            self.n += 1
            return {"results": [], "_links": {"next": "/x"}}

    client = _AlwaysNext()
    source = ConfluenceTrashSource(client=client, space_keys=["S"], max_pages=3)

    source.list_trashed_ids()

    assert client.n == 3


class _RecordingTransport:
    """get_json 호출의 url/headers 를 기록하는 fake transport."""

    def __init__(self) -> None:
        self.url: str = ""
        self.headers: dict[str, str] = {}

    def get_json(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        self.url = url
        self.headers = headers
        return {"results": [], "_links": {}}


def test_trash_content_client_builds_initial_url_and_auth_headers() -> None:
    # OAuth(비 admin-key) 경로 — Bearer + 게이트웨이 URL (종전 동작 보존).
    transport = _RecordingTransport()
    client = ConfluenceTrashContentClient(
        cloud_id="CID",
        access_token="secret-token",
        page_limit=50,
        transport=transport,
    )

    client.fetch_trashed(space_key="CLOUD", cursor=None)

    assert transport.url.startswith(
        "https://api.atlassian.com/ex/confluence/CID/wiki/rest/api/content?"
    )
    assert "status=trashed" in transport.url
    assert "spaceKey=CLOUD" in transport.url
    assert "limit=50" in transport.url
    assert transport.headers["Authorization"] == "Bearer secret-token"
    assert "Atl-Confluence-With-Admin-Key" not in transport.headers


def test_trash_content_client_admin_mode_uses_basic_and_site_url() -> None:
    # admin-key 경로(api-spec v2.6.1, 2026-06-11 정정) — Basic + site URL. 종전
    # Bearer+게이트웨이+Admin-Key 조합은 admin-key 가 동작하지 않아 폐기됐다.
    transport = _RecordingTransport()
    client = ConfluenceTrashContentClient(
        cloud_id="CID",
        access_token="",
        use_admin_key=True,
        page_limit=50,
        transport=transport,
        site_url="https://lina.atlassian.net",
        admin_authorization="Basic c2VjcmV0",
    )

    client.fetch_trashed(space_key="CLOUD", cursor=None)

    assert transport.url.startswith("https://lina.atlassian.net/wiki/rest/api/content?")
    assert transport.headers["Authorization"] == "Basic c2VjcmV0"
    assert transport.headers["Atl-Confluence-With-Admin-Key"] == "true"


def test_trash_content_client_absolutizes_links_next_cursor() -> None:
    transport = _RecordingTransport()
    client = ConfluenceTrashContentClient(
        cloud_id="CID",
        access_token="t",
        transport=transport,
    )

    client.fetch_trashed(space_key="CLOUD", cursor="/wiki/rest/api/content?status=trashed&start=1")

    # _links.next 상대경로(/wiki/...)는 /ex/confluence/{cloud_id} prefix 로 절대화한다.
    assert transport.url == (
        "https://api.atlassian.com/ex/confluence/CID/wiki/rest/api/content?status=trashed&start=1"
    )
    # admin key 미설정 시 헤더 없음.
    assert "Atl-Confluence-With-Admin-Key" not in transport.headers
