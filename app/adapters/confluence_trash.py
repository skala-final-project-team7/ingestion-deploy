"""Confluence Trash 소스 — 삭제(Trashed) 페이지/첨부 id 수집 (FR-005 / featureI-5b).

--------------------------------------------------
작성자 : 최태성
담당 영역 : ingestion
작성목적 : 3중 삭제 동기화 2번째 안전망(Trash API). Confluence ``status=trashed`` content 를
          조회해 삭제된 page_id/attachment_id 를 산출한다. Sync Worker 가 이 결과를
          ``apply_soft_deletes`` 로 넘겨 Qdrant ``is_deleted=true`` 를 set 한다(주1회 →
          1시간 안전망). raw HTTP 는 ``TrashContentClient`` 로 주입해(테스트는 fake) 본
          모듈의 파싱·페이지네이션 로직만 단위테스트한다. vendored 패키지는 무수정이며,
          실 HTTP 는 ``ConfluenceTrashContentClient``(urllib)로 본 어댑터가 직접 호출한다.
작성일 : 2026-06-04 (featureI-5b — 3중 삭제 트리거 배선)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-06-04, 최초 작성, featureI-5b — TrashedIds / TrashSource(+Fake) / parse_trashed_content /
    ConfluenceTrashSource(페이지네이션) / ConfluenceTrashContentClient(실 urllib).
--------------------------------------------------
[보안] access_token 은 로그·예외 메시지에 남기지 않는다(루트 CLAUDE.md 보안 규칙).
[호환성]
  - Python 3.11.x
  - 실 HTTP 는 urllib(표준 라이브러리). 파싱·순회는 외부 의존성 0.
--------------------------------------------------
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

# vendored Confluence client 와 동일 origin — Atlassian Cloud REST gateway.
_CONFLUENCE_API_ORIGIN = "https://api.atlassian.com"


@dataclass(frozen=True, slots=True)
class TrashedIds:
    """Trash 조회 결과 — 삭제(Trashed)된 page/attachment id 집합."""

    pages: set[str] = field(default_factory=set)
    attachments: set[str] = field(default_factory=set)

    @property
    def is_empty(self) -> bool:
        """삭제 대상이 하나도 없으면 True(불필요한 soft_delete 호출 회피용)."""
        return not self.pages and not self.attachments


class TrashSource(Protocol):
    """Trash 소스 seam — Sync Worker 가 ``list_trashed_ids`` 만 호출한다."""

    def list_trashed_ids(self) -> TrashedIds:
        """현재 Trashed 상태인 page_id/attachment_id 를 반환한다."""
        ...


@dataclass(frozen=True, slots=True)
class FakeTrashSource:
    """테스트용 TrashSource — 고정 ``TrashedIds`` 를 반환한다."""

    trashed: TrashedIds = field(default_factory=TrashedIds)

    def list_trashed_ids(self) -> TrashedIds:
        return self.trashed


def parse_trashed_content(results: Iterable[Any]) -> TrashedIds:
    """Confluence content 목록(``status=trashed``)에서 page/attachment id 를 분류 추출한다.

    각 항목 ``type`` 이 ``attachment`` 면 attachment, 그 외(``page`` 등)는 page 로 분류한다.
    ``id`` 누락·비-dict·명시적 non-trashed status 는 graceful 하게 건너뛴다(전체 실패 금지).

    Args:
        results: Confluence ``/content?status=trashed`` 응답의 ``results`` 배열.

    Returns:
        분류된 ``TrashedIds``.
    """
    pages: set[str] = set()
    attachments: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        content_id = str(item.get("id") or "").strip()
        if not content_id:
            continue
        status = str(item.get("status") or "").strip().lower()
        if status and status != "trashed":
            continue
        content_type = str(item.get("type") or "page").strip().lower()
        if content_type == "attachment":
            attachments.add(content_id)
        else:
            pages.add(content_id)
    return TrashedIds(pages=pages, attachments=attachments)


class TrashContentClient(Protocol):
    """Confluence trashed content 조회 raw 클라이언트 — 페이지네이션 1스텝."""

    def fetch_trashed(self, *, space_key: str, cursor: str | None) -> dict[str, Any]:
        """``status=trashed`` content 한 페이지를 반환한다.

        Args:
            space_key: 조회 대상 스페이스 키.
            cursor: 이전 응답의 ``_links.next``(없으면 None — 첫 페이지).

        Returns:
            ``{"results": [...], "_links": {"next": "<path>"|None}}`` 형태의 응답 body.
        """
        ...


@dataclass(frozen=True, slots=True)
class ConfluenceTrashSource:
    """Confluence Trash API 로 삭제 page/attachment id 를 수집한다(스페이스별 순회).

    raw HTTP 는 ``TrashContentClient`` 로 주입(테스트는 fake). 본 클래스는 스페이스별
    ``_links.next`` 페이지네이션 순회 + ``parse_trashed_content`` 누적만 담당한다.
    ``max_pages`` 로 무한 루프를 방지한다. 빈 ``space_keys`` 면 빈 결과.

    Attributes:
        client: trashed content 조회 raw 클라이언트(주입).
        space_keys: 조회 대상 스페이스 키 목록.
        max_pages: 스페이스당 페이지네이션 안전 상한(기본 100).
    """

    client: TrashContentClient
    space_keys: Sequence[str]
    max_pages: int = 100

    def list_trashed_ids(self) -> TrashedIds:
        pages: set[str] = set()
        attachments: set[str] = set()
        for space_key in self.space_keys:
            cursor: str | None = None
            for _ in range(self.max_pages):
                body = self.client.fetch_trashed(space_key=space_key, cursor=cursor)
                batch = parse_trashed_content(body.get("results") or [])
                pages |= batch.pages
                attachments |= batch.attachments
                cursor = (body.get("_links") or {}).get("next")
                if not cursor:
                    break
        return TrashedIds(pages=pages, attachments=attachments)


class _TrashHttpTransport(Protocol):
    """실 HTTP GET seam — 테스트는 fake 로 교체해 네트워크 없이 URL/파싱을 검증한다."""

    def get_json(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        """``url`` 을 GET 해 JSON body 를 dict 로 반환한다."""
        ...


class _UrllibTrashHttpTransport:
    """urllib 기반 기본 transport(운영용). 네트워크 호출은 Mac/통합 환경에서 검증한다."""

    def __init__(self, *, timeout_seconds: int = 20) -> None:
        self._timeout_seconds = timeout_seconds

    def get_json(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        request = Request(url, headers=headers, method="GET")
        with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310 — https 고정
            body: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        return body


@dataclass(frozen=True, slots=True)
class ConfluenceTrashContentClient:
    """``status=trashed`` content 를 Confluence v1 REST 로 조회하는 실 클라이언트(urllib).

    URL 구성·인증 헤더·``_links.next`` 처리만 담당한다. 실제 네트워크는
    ``_TrashHttpTransport`` 로 주입해(기본 urllib) 단위테스트에서 fake 로 대체 가능하다.
    credential 은 로그·예외에 남기지 않는다.

    자격증명 모델(v2.6.1 정정 → v2.6.2 ML 측 단서로 보존): ``use_admin_key=True`` 면
    ``admin_authorization``(``Basic base64(adminEmail:adminApiToken)``) + Admin Key 헤더를
    **site URL**(``site_url``) 에서 사용한다 — OAuth Bearer/게이트웨이 경로에서는 admin-key
    가 동작하지 않는다(위반 시 trashed 페이지 무음 누락). False 면 종전 Bearer 게이트웨이.

    Attributes:
        cloud_id: Confluence cloudId (OAuth 게이트웨이 경로용).
        access_token: OAuth access token(로그 금지 — admin-key 경로에서는 미사용).
        use_admin_key: Admin Key header 포함 여부(권한 무관 전수 조회 시).
        page_limit: 페이지당 ``limit`` (기본 100).
        transport: HTTP GET seam(주입). None 이면 urllib 기본 transport.
        site_url: admin-key 경로의 site origin(예: https://{site}.atlassian.net).
        admin_authorization: admin-key 경로의 Authorization 헤더 값(Basic ...).
    """

    cloud_id: str
    access_token: str
    use_admin_key: bool = False
    page_limit: int = 100
    transport: _TrashHttpTransport | None = None
    site_url: str = ""
    admin_authorization: str = ""

    def __post_init__(self) -> None:
        if self.use_admin_key and not (self.site_url and self.admin_authorization):
            raise ValueError(
                "use_admin_key=True 에는 site_url/admin_authorization 이 필수다 — admin-key 는 "
                "admin API Token 의 Basic 인증으로만 site URL 에서 동작한다(api-spec v2.6.1)"
            )

    @classmethod
    def from_settings(cls, settings: Any) -> ConfluenceTrashContentClient:
        """Settings 로 모드별 클라이언트를 조립한다(admin-key: Basic+site / OAuth: Bearer)."""
        if settings.atlassian_use_admin_key:
            from app.adapters.atlassian import build_admin_basic_authorization

            return cls(
                cloud_id=settings.atlassian_cloud_id,
                access_token="",
                use_admin_key=True,
                site_url=settings.atlassian_site_url.rstrip("/"),
                admin_authorization=build_admin_basic_authorization(
                    settings.atlassian_admin_email,
                    settings.atlassian_admin_api_token.get_secret_value(),
                ),
            )
        return cls(
            cloud_id=settings.atlassian_cloud_id,
            access_token=settings.atlassian_access_token.get_secret_value(),
        )

    def fetch_trashed(self, *, space_key: str, cursor: str | None) -> dict[str, Any]:
        transport = self.transport or _UrllibTrashHttpTransport()
        url = self._absolutize(cursor) if cursor else self._initial_url(space_key)
        return transport.get_json(url, self._headers())

    def _wiki_base(self) -> str:
        if self.site_url:
            return f"{self.site_url}/wiki"
        return f"{_CONFLUENCE_API_ORIGIN}/ex/confluence/{self.cloud_id}/wiki"

    def _initial_url(self, space_key: str) -> str:
        query = urlencode(
            {
                "status": "trashed",
                "spaceKey": space_key,
                "limit": self.page_limit,
                "expand": "container",
            }
        )
        return f"{self._wiki_base()}/rest/api/content?{query}"

    def _absolutize(self, next_path: str) -> str:
        # ``_links.next`` 는 보통 ``/wiki/rest/api/...`` 상대 경로다 — origin 에 결합한다.
        if next_path.startswith("http://") or next_path.startswith("https://"):
            return next_path
        if next_path.startswith("/wiki/"):
            if self.site_url:
                return f"{self.site_url}{next_path}"
            return urljoin(_CONFLUENCE_API_ORIGIN, f"/ex/confluence/{self.cloud_id}{next_path}")
        return f"{self._wiki_base()}{next_path}"

    def _headers(self) -> dict[str, str]:
        # admin-key 경로(v2.6.1): Basic + Admin Key 헤더 — Bearer 로는 admin-key 미동작.
        if self.use_admin_key:
            return {
                "Accept": "application/json",
                "Authorization": self.admin_authorization,
                "Atl-Confluence-With-Admin-Key": "true",
            }
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }
