"""Atlassian Document Source Adapter — vendored Data Ingestion Agent 연결 [Agent 경계].

--------------------------------------------------
작성자 : 최태성
작성목적 : 저장소 루트에 무수정 vendoring 된 Data Ingestion Agent(FR-001 Confluence Full
          Crawl)를 ``DocumentSourceAdapter`` 계약으로 감싼다. 에이전트는 자체
          ``ProcessedDocument`` 스키마(space/page/body/metadata 중첩)를 산출하므로, 본
          어댑터가 이를 ingestion 표준 ``PageObject`` 로 변환한다(vendored 무수정 보존,
          모든 변환은 어댑터에서 수행). 파이프라인 본체(crawler/sync)는 어떤 공급원인지
          알지 못한 채 표준 PageObject 스트림만 소비한다.
작성일 : 2026-05-26
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-26, 최초 작성, featureI-6 — vendored Data Ingestion Agent in-process
    호출(run_full_crawl_workflow 블랙박스) + ProcessedDocument→PageObject 매핑 +
    space_key 기반 PoC ACL 합성. 2026-06-02 Admin Key 실측으로 page-level read
    restriction API 적용 가능성을 확인했으며, 운영 ACL 수집은 후속 작업으로 분리.
  - 2026-06-04, ACL 운영 연동 + allow_authenticated 정책 — Admin Key 기반 page-level
    read restriction 수집(``ConfluenceRestrictionAclProvider``)을 반영하고, restriction
    이 비어 있을 때 "모든 인증 사용자" sentinel group(``atlassian_public_acl_group``)을
    부여하는 ``allow_authenticated`` 정책을 추가한다(space key 불필요 — Full Crawl 은
    Admin Key 로 접근 가능한 전체 스페이스를 수집). sentinel 매칭은 RAG 검색
    ``build_acl_filter`` 가 모든 principal 에 동일 토큰을 주입해야 성립한다(공유 계약 —
    docs/db-schema.md §1.4, ADR 0003).
  - 2026-06-10, 코드 리뷰 재점검(A2·A3·A16) — empty_restriction 기본 정책 문서를 mark_missing
    (fail-closed) 기준으로 정정하고, build_restriction_acl_provider() 를 추출해 full crawl
    (from_settings)과 delta(bootstrap.build_delta_runner)가 동일 ACL 산출 경로를 공유하게 함.
  - 2026-06-10, A2 후속·A8 잔여 — (1) **ancestor restriction 상속 워크 구현**: 빈 page
    restriction 시 조상 체인(full crawl 은 payload parent_id 체인, delta 는 get_page_detail
    parentId 워크)을 가까운 순으로 조회해 상속 ACL 산출(+restriction/parent 메모이즈,
    ancestor_lookup_enabled 토글). 어댑터는 크롤 1회 수집으로 parent/title 맵을 만들어
    ancestor_ids(ACL)와 ancestors 제목 체인(section_path)을 함께 채운다. (2) SpaceInfo
    id/name → PageObject.space_id/space_name 매핑(sources[].spaceId/spaceName 원천).
--------------------------------------------------
[호환성]
  - Python 3.11.x (vendored 에이전트가 enum.StrEnum 사용)
  - vendored ``data_ingestion_agent`` 패키지(저장소 루트) 필요
--------------------------------------------------
[ACL 수집 정책 (docs/db-schema.md §1.4 / docs/atlassian-api.md)]
  - ``atlassian_use_admin_key=False`` (기본): page-level restriction API 미사용. PoC
    ``synthesize_space_acl`` 로 ``["space:{space_key}"]`` 합성(기존 동작 보존).
  - ``atlassian_use_admin_key=True``: Admin Key 로 ``/rest/api/content/{pageId}/
    restriction/byOperation/read`` 를 조회해 ``allowed_groups``/``allowed_users`` 산출.
    restriction 이 비어 있을 때의 처리는 ``atlassian_empty_restriction_policy`` 로 분기:
      * ``allow_authenticated`` (opt-in — 기본은 ``mark_missing``): ``[public_acl_group]`` 부여
        → 모든 인증 사용자 허용(상속 제한 미반영 위험 — A2).
      * ``space_fallback``: ``["space:{space_key}"]`` 합성(공간 단위 ACL).
      * ``mark_missing``: 빈 ACL → 색인 단계 ``INVALID_ACL`` 차단(보수 정책).
--------------------------------------------------
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from app.adapters.base import ActiveIds, ChangeEvent, DocumentSourceAdapter
from app.adapters.json_fixture import parse_atlassian_datetime
from app.schemas.page_object import PageObject

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.config import Settings

EMPTY_RESTRICTION_POLICIES = frozenset({"mark_missing", "space_fallback", "allow_authenticated"})


class _WorkflowRunner(Protocol):
    """vendored full crawl workflow 호출 시그니처 — 테스트 주입 지점."""

    def __call__(self, *, config: Any, client: Any | None = None) -> Any:
        """Full crawl workflow 를 실행하고 ``.documents`` 를 가진 결과를 반환한다."""


class PageAclProvider(Protocol):
    """페이지별 ACL을 반환하는 provider seam."""

    def get_page_acl(self, *, page_id: str, space_key: str) -> tuple[list[str], list[str]]:
        """allowed_groups, allowed_users를 반환한다."""
        ...


@dataclass(frozen=True, slots=True)
class ConfluenceRestrictionAclProvider:
    """Confluence read restriction 응답을 PageObject ACL payload로 변환한다.

    Empty restriction은 곧 "공개"를 의미한다고 단정할 수 없다. Admin Key 실측에서
    page-level restriction이 비어도 상위 folder/page/space 권한 때문에 일반 조회가 막히는
    사례가 확인됐다. 따라서 빈 restriction 처리는 ``empty_restriction_policy`` 로 명시 분기한다.

    정책별 동작:
      - ``mark_missing`` (기본 — Settings 기본값과 동일): 빈 ACL 반환 → 색인 ACL gate 가
        차단(fail-closed). 위 실측 사례 때문에 ancestor restriction 조회가 구현되기
        전까지는 이것이 안전 기본값이다(코드 리뷰 A2).
      - ``space_fallback``: ``synthesize_space_acl`` 로 공간 단위 ACL 합성.
      - ``allow_authenticated`` (opt-in 전용): ``[public_acl_group]`` 부여(모든 인증 사용자
        허용 sentinel). 상속 제한 문서를 과다 노출할 수 있으므로 명시 opt-in 으로만 사용.
    """

    client: Any
    # 값 후보는 EMPTY_RESTRICTION_POLICIES 참조.
    empty_restriction_policy: str = "mark_missing"
    group_identifier_fields: tuple[str, ...] = ("id", "groupId", "name")
    group_acl_prefix: str = ""
    public_acl_group: str = "*"
    # 2026-06-10(A2 후속) — 빈 page restriction 시 조상 체인의 restriction 을 조회해
    # 상속 ACL 을 산출한다(가까운 조상 우선). False 면 종전 동작(정책 폴백 직행).
    ancestor_lookup_enabled: bool = True
    max_ancestor_depth: int = 20
    # 호출 캐시 — restriction/parent 조회는 페이지·조상 단위로 1회만 수행한다(크롤 중
    # 형제 페이지들이 같은 조상을 공유하므로 API 비용을 조상 수로 상한). frozen 이지만
    # dict 내용 변이는 허용된다(eq/repr 제외).
    _restriction_cache: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _parent_cache: dict[str, str | None] = field(default_factory=dict, repr=False, compare=False)

    # 어댑터가 크롤 시점에 확보한 조상 체인(ancestor_ids)을 전달해도 되는 provider 임을
    # 알리는 capability 플래그 — 테스트용 단순 fake(2-kwarg)와의 호환을 위해 명시 opt-in.
    supports_ancestor_ids: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "empty_restriction_policy",
            parse_empty_restriction_policy(self.empty_restriction_policy),
        )

    def reset_cache(self) -> None:
        """restriction/parent 메모이즈 캐시를 비운다 — **수집 런 시작마다 호출**.

        provider 는 startup 1회 생성되어 잡 간 재사용되므로(``api/deps.py``,
        ``bootstrap.build_delta_runner``), 캐시를 런 단위로 비우지 않으면 Confluence 에서
        변경된 restriction 이 재수집에 반영되지 않는다(이전 ACL 잔존 — over/under-grant).
        full crawl 은 ``AtlassianSourceAdapter.fetch_pages``, delta 는 ``run_delta_sync``
        진입 시 호출한다. 캐시는 한 런 내부에서만 API 비용을 조상 수로 상한한다.
        """
        self._restriction_cache.clear()
        self._parent_cache.clear()

    def get_page_acl(
        self,
        *,
        page_id: str,
        space_key: str,
        ancestor_ids: Sequence[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        """page-level → (빈 경우) 조상 체인 → (모두 빈 경우) 정책 폴백 순으로 ACL 산출.

        Args:
            page_id: 대상 페이지.
            space_key: 폴백(``space_fallback``) 합성용.
            ancestor_ids: 가까운 조상 → 루트 순 조상 id. 호출자(full crawl 어댑터)가
                크롤 payload 의 parent_id 체인으로 전달하면 API 추가 호출 없이 사용한다.
                None 이면(delta 등) ``get_page_detail`` 의 parentId 를 따라 직접 걷는다.
        """
        allowed_groups, allowed_users = self._restrictions_for(page_id)
        if allowed_groups or allowed_users:
            return list(allowed_groups), list(allowed_users)

        if self.ancestor_lookup_enabled:
            for ancestor_id in self._resolve_ancestor_ids(page_id, ancestor_ids):
                a_groups, a_users = self._restrictions_for(ancestor_id)
                if a_groups or a_users:
                    # Confluence view restriction 은 하위 페이지에 상속된다 — 가장
                    # 가까운 제한 조상의 ACL 을 본 페이지의 유효 ACL 로 사용한다.
                    return list(a_groups), list(a_users)

        if self.empty_restriction_policy == "allow_authenticated":
            return synthesize_authenticated_acl(self.public_acl_group)
        if self.empty_restriction_policy == "space_fallback":
            return synthesize_space_acl(space_key)
        return [], []

    def _restrictions_for(self, page_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """restriction API 호출(+파싱) — 페이지 단위 메모이즈."""
        cached = self._restriction_cache.get(page_id)
        if cached is not None:
            return cached
        raw = self.client.get_page_read_restrictions(page_id)
        groups, users = parse_read_restrictions_acl(
            raw,
            group_identifier_fields=self.group_identifier_fields,
            group_acl_prefix=self.group_acl_prefix,
        )
        result = (tuple(groups), tuple(users))
        self._restriction_cache[page_id] = result
        return result

    def _resolve_ancestor_ids(self, page_id: str, provided: Sequence[str] | None) -> Iterator[str]:
        """조상 id 를 가까운 순으로 산출 — 전달분 우선, 없으면 parentId API 워크."""
        if provided is not None:
            seen: set[str] = {page_id}
            for ancestor_id in provided:
                if ancestor_id and ancestor_id not in seen:
                    seen.add(ancestor_id)
                    yield ancestor_id
            return
        current = page_id
        visited = {page_id}
        for _ in range(self.max_ancestor_depth):
            parent = self._parent_of(current)
            if not parent or parent in visited:
                return
            visited.add(parent)
            yield parent
            current = parent

    def _parent_of(self, page_id: str) -> str | None:
        """v2 page 상세의 parentId — 페이지 단위 메모이즈. 조회 실패는 None(정책 폴백)."""
        if page_id in self._parent_cache:
            return self._parent_cache[page_id]
        detail_fn = getattr(self.client, "get_page_detail", None)
        parent: str | None = None
        if detail_fn is not None:
            try:
                detail = detail_fn(page_id)
                raw_parent = detail.get("parentId") or detail.get("parent_id")
                parent = str(raw_parent) if raw_parent else None
            except Exception:  # noqa: BLE001 — 조상 조회 실패가 수집을 중단시키지 않는다.
                _LOGGER.warning(
                    "ancestor lookup failed for page_id=%s — 정책 폴백으로 진행",
                    page_id,
                    exc_info=True,
                )
                parent = None
        self._parent_cache[page_id] = parent
        return parent


def build_restriction_acl_provider(settings: Settings) -> ConfluenceRestrictionAclProvider | None:
    """Settings 기반 page-level ACL provider 빌더 — full crawl/delta 공용 seam(코드 리뷰 A3).

    ``atlassian_use_admin_key=False`` 면 None(PoC space_key 합성 폴백). True 면 Admin Key
    클라이언트로 read restriction 을 조회하는 provider 를 만든다. ``from_settings``(full
    crawl)와 ``bootstrap.build_delta_runner``(delta)가 같은 빌더를 사용해 두 경로의 ACL
    산출을 통일한다.
    """
    if not settings.atlassian_use_admin_key:
        return None
    return ConfluenceRestrictionAclProvider(
        client=_default_confluence_client(
            cloud_id=settings.atlassian_cloud_id,
            access_token=settings.atlassian_access_token.get_secret_value(),
            request_delay_seconds=settings.atlassian_request_delay_seconds,
            max_retries=settings.atlassian_max_retries,
            timeout_seconds=settings.atlassian_timeout_seconds,
            use_admin_key=settings.atlassian_use_admin_key,
        ),
        group_identifier_fields=parse_group_identifier_fields(
            settings.atlassian_group_acl_field_order
        ),
        group_acl_prefix=settings.atlassian_group_acl_prefix,
        empty_restriction_policy=parse_empty_restriction_policy(
            settings.atlassian_empty_restriction_policy
        ),
        public_acl_group=settings.atlassian_public_acl_group,
        ancestor_lookup_enabled=settings.atlassian_ancestor_restriction_lookup,
    )


class AtlassianSourceAdapter(DocumentSourceAdapter):
    """vendored Data Ingestion Agent 를 ``DocumentSourceAdapter`` 로 감싼 어댑터.

    Full Crawl 은 vendored ``run_full_crawl_workflow`` 를 in-process 로 호출(블랙박스)하고,
    산출 ``ProcessedDocument`` 목록을 표준 ``PageObject`` 로 변환한다. 에이전트는 로컬
    파일로 산출물을 쓰므로 임시 디렉토리로 출력을 우회하고(파이프라인은 MongoDB
    ``raw_pages`` 에 적재), 메모리 결과(``result.documents``)만 소비한다.

    Args:
        cloud_id: Atlassian Cloud ID(외부 주입). 빈 값이면 실행 시 에이전트가 검증 실패.
        access_token: Confluence access token(외부 주입). 로그·메시지에 남기지 않는다.
        client: vendored 에이전트의 Confluence client. None 이면 에이전트가 운영용
            ``ConfluenceClient`` 를 생성한다. 테스트는 fake client 를 주입한다.
        acl_provider: page-level ACL provider(seam). None 이면 PoC space_key 합성으로
            폴백한다. ``from_settings`` 는 ``atlassian_use_admin_key`` 가 켜진 경우에만
            ``ConfluenceRestrictionAclProvider`` 를 주입한다.
        workflow_runner: full crawl workflow 호출자. 기본값은 vendored
            ``run_full_crawl_workflow``. 테스트에서 교체 가능.
        request_delay_seconds / max_retries / timeout_seconds: 에이전트 호출 속도·재시도 설정.
        use_admin_key: vendored 에이전트 Confluence 요청에 Admin Key header 포함 여부.
    """

    def __init__(
        self,
        *,
        cloud_id: str,
        access_token: str,
        client: Any | None = None,
        acl_provider: PageAclProvider | None = None,
        workflow_runner: _WorkflowRunner | None = None,
        request_delay_seconds: float = 0.3,
        max_retries: int = 3,
        timeout_seconds: int = 20,
        use_admin_key: bool = False,
    ) -> None:
        self._cloud_id = cloud_id
        self._access_token = access_token
        self._client = client
        self._acl_provider = acl_provider
        self._workflow_runner = workflow_runner
        self._request_delay_seconds = request_delay_seconds
        self._max_retries = max_retries
        self._timeout_seconds = timeout_seconds
        self._use_admin_key = use_admin_key

    @classmethod
    def from_settings(cls, settings: Settings) -> AtlassianSourceAdapter:
        """Settings 의 placeholder 자격증명으로 어댑터를 생성한다(팩토리 경로).

        access_token/cloud_id 전달 경로가 확정되기 전 PoC placeholder 다. 자격증명이
        비어 있으면 실행 시 vendored 에이전트의 config 검증에서 실패한다.

        ``atlassian_use_admin_key`` 가 켜져 있으면 Admin Key 로 page-level read restriction
        을 조회하는 ``ConfluenceRestrictionAclProvider`` 를 주입한다(빈 restriction 처리는
        ``atlassian_empty_restriction_policy`` 로 분기). 꺼져 있으면 provider 를 주입하지
        않아 PoC space_key 합성으로 동작한다(기존 동작 보존).
        """
        acl_provider = build_restriction_acl_provider(settings)
        return cls(
            cloud_id=settings.atlassian_cloud_id,
            access_token=settings.atlassian_access_token.get_secret_value(),
            acl_provider=acl_provider,
            request_delay_seconds=settings.atlassian_request_delay_seconds,
            max_retries=settings.atlassian_max_retries,
            timeout_seconds=settings.atlassian_timeout_seconds,
            use_admin_key=settings.atlassian_use_admin_key,
        )

    # --- DocumentSourceAdapter 인터페이스 ---

    def fetch_pages(self, since: datetime | None = None) -> Iterator[PageObject]:
        """vendored Full Crawl 을 실행하고 표준 PageObject 스트림으로 변환해 반환한다.

        Args:
            since: 지정 시 ``last_modified`` 가 since 이후인 페이지만 반환(증분).
                None 이면 전체(Full Crawl). 에이전트 MVP 는 항상 전체를 수집하므로
                증분 필터는 어댑터에서 ``last_modified`` 비교로 적용한다.
        """
        # 런 단위 ACL 캐시 초기화 — provider 가 잡 간 재사용되므로, 직전 크롤이 캐시한
        # restriction 이 이번 런의 ACL 산출에 재사용되지 않게 한다(권한 변경 반영).
        reset_cache = getattr(self._acl_provider, "reset_cache", None)
        if callable(reset_cache):
            reset_cache()
        documents = self._collect_documents()
        # 2026-06-10(A2 후속) — 크롤 payload 의 parent_id 로 조상 체인을 로컬 구성한다.
        # provider 의 ancestor restriction 워크가 추가 API 호출 없이 동작하게 하고,
        # PageObject.ancestors(섹션 경로용 제목 체인)도 함께 채운다.
        parent_by_id = {d.page.page_id: d.page.parent_id for d in documents}
        title_by_id = {d.page.page_id: d.page.title for d in documents}
        for document in documents:
            page = self._to_page_object(
                document, parent_by_id=parent_by_id, title_by_id=title_by_id
            )
            if since is not None and page.last_modified < since:
                continue
            yield page

    def list_active_ids(self) -> ActiveIds:
        """공급원에 현재 살아있는 페이지 ID 집합(Reconciliation 대조용).

        에이전트 MVP 는 첨부를 수집하지 않으므로 ``attachments`` 는 빈 집합이다.
        """
        ids = ActiveIds()
        for document in self._collect_documents():
            ids.pages.add(document.page.page_id)
        return ids

    def watch_changes(self) -> Iterator[ChangeEvent]:
        """실시간 변경 이벤트 — 에이전트 MVP 는 Webhook 미지원이라 빈 스트림."""
        yield from ()

    # --- 내부 헬퍼 ---

    def _collect_documents(self) -> list[Any]:
        """vendored full crawl workflow 를 1회 실행해 ProcessedDocument 목록을 반환한다.

        에이전트는 산출물을 로컬 파일로 쓰므로 임시 디렉토리로 우회하고(즉시 정리),
        메모리 결과만 사용한다. 파이프라인 적재(raw_pages)는 crawler 가 담당한다.
        """
        runner = self._workflow_runner or _default_workflow_runner()
        config = self._build_config(output_dir=tempfile.mkdtemp(prefix="ingestion-agent-"))
        try:
            result = runner(config=config, client=self._client)
            return list(result.documents)
        finally:
            shutil.rmtree(str(config.output_dir), ignore_errors=True)

    def _build_config(self, *, output_dir: str) -> Any:
        from data_ingestion_agent.config import DataIngestionConfig

        return DataIngestionConfig(
            cloud_id=self._cloud_id,
            access_token=self._access_token,
            output_dir=output_dir,
            request_delay_seconds=self._request_delay_seconds,
            max_retries=self._max_retries,
            timeout_seconds=self._timeout_seconds,
            use_admin_key=self._use_admin_key,
        )

    def _to_page_object(
        self,
        document: Any,
        *,
        parent_by_id: dict[str, str | None] | None = None,
        title_by_id: dict[str, str] | None = None,
    ) -> PageObject:
        """vendored ProcessedDocument → 표준 PageObject 변환(모든 변환은 어댑터에서).

        매핑(docs/atlassian-api.md 매핑표 + 에이전트 ProcessedDocument 스키마):
            page.page_id            → page_id
            space.space_key/id/name → space_key / space_id / space_name (A8 — 출처 카드)
            page.title              → title
            body.storage_html       → body_html  (청커가 HTML 파싱)
            page.version_number     → version_number
            page.last_modified_at   → last_modified (ISO 8601 파싱)
            page.page_url           → webui_link
            page.parent_id 체인      → ancestors(제목, 루트→직계) + ACL 조상 워크(A2)
            restriction API         → allowed_groups/allowed_users (Admin Key 경로)
            (admin key off)         → allowed_groups/allowed_users (PoC space 합성)
            (MVP 미산출)            → labels=[] / attachments=[]
        """
        space_key = document.space.space_key
        page_id = document.page.page_id
        ancestor_ids = _ancestor_chain(page_id, parent_by_id or {})
        allowed_groups, allowed_users = self._resolve_acl(
            page_id=page_id, space_key=space_key, ancestor_ids=ancestor_ids
        )
        titles = title_by_id or {}
        # ancestors 는 섹션 경로(section_path)용 제목 체인 — 루트→직계 순(metadata.py 정합).
        # 크롤 배치에 제목이 없는 조상(homepage 등 수집 범위 밖)은 경로에서 생략한다 —
        # ACL 워크(ancestor_ids)는 id 기준이라 영향 없음.
        ancestor_titles = [titles[aid] for aid in reversed(ancestor_ids) if aid in titles]
        return PageObject(
            page_id=page_id,
            space_key=space_key,
            space_id=str(getattr(document.space, "space_id", "") or ""),
            space_name=str(getattr(document.space, "space_name", "") or ""),
            title=document.page.title,
            body_html=document.body.storage_html,
            version_number=document.page.version_number,
            last_modified=_parse_last_modified(document.page.last_modified_at),
            allowed_groups=allowed_groups,
            allowed_users=allowed_users,
            webui_link=document.page.page_url,
            labels=[],
            ancestors=ancestor_titles,
            attachments=[],
        )

    def _synthesize_acl(self, space_key: str) -> tuple[list[str], list[str]]:
        """PoC ACL 합성 — space_key 기반 그룹(JsonFixtureSourceAdapter 패턴 동일).

        ``atlassian_use_admin_key`` 가 꺼져 있어 ACL provider 가 없을 때의 폴백이다.
        page-level restriction 이 비어 있는 경우 상위 folder/page/space permission 처리
        정책은 ``ConfluenceRestrictionAclProvider`` 로 옮겼다(docs/db-schema.md §1.4, ADR 0003).
        """
        return synthesize_space_acl(space_key)

    def _resolve_acl(
        self,
        *,
        page_id: str,
        space_key: str,
        ancestor_ids: Sequence[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        if self._acl_provider is None:
            return self._synthesize_acl(space_key)
        # restriction 조회 하드 실패(재시도 소진 등)는 페이지 단위로 격리한다 — 1건이
        # full crawl 잡 전체를 FAILED 로 만들지 않도록 빈 ACL 로 fail-closed 강등하고
        # (빈 ACL 페이지는 chunking 의 INVALID_ACL 게이트가 색인에서 제외 — app/CLAUDE.md
        # §3), delta 경로의 페이지 단위 격리(A13, sync.py failed_items)와 정합한다.
        try:
            # ancestor_ids 는 capability 플래그(supports_ancestor_ids)를 켠 provider 에만
            # 전달한다 — 2-kwarg 시그니처의 기존/테스트 fake provider 호환(A2 후속).
            # Protocol(PageAclProvider)은 공통 2-kwarg 계약만 정의하므로, capability 확인
            # 후의 확장 호출은 Any 로 좁혀 호출한다(런타임 게이트가 시그니처를 보장).
            if ancestor_ids is not None and getattr(
                self._acl_provider, "supports_ancestor_ids", False
            ):
                ancestor_capable: Any = self._acl_provider
                return ancestor_capable.get_page_acl(
                    page_id=page_id, space_key=space_key, ancestor_ids=ancestor_ids
                )
            return self._acl_provider.get_page_acl(page_id=page_id, space_key=space_key)
        except Exception:  # noqa: BLE001 — restriction API 실패는 페이지 단위 fail-closed 격리
            _LOGGER.warning(
                "ACL restriction lookup failed for page_id=%s — 빈 ACL(fail-closed)로 "
                "강등해 색인에서 제외되도록 한다(INVALID_ACL 게이트)",
                page_id,
                exc_info=True,
            )
            return [], []


def parse_read_restrictions_acl(
    raw: dict[str, Any],
    *,
    group_identifier_fields: tuple[str, ...] = ("id", "groupId", "name"),
    group_acl_prefix: str = "",
) -> tuple[list[str], list[str]]:
    """Confluence read restriction 응답에서 allowed_groups/users를 추출한다."""
    restrictions = raw.get("restrictions")
    if not isinstance(restrictions, dict):
        return [], []

    group_results = _restriction_results(restrictions.get("group"))
    user_results = _restriction_results(restrictions.get("user"))
    groups = [
        _with_prefix(_group_acl_value(group, group_identifier_fields), group_acl_prefix)
        for group in group_results
    ]
    users = [
        str(user.get("accountId")).strip()
        for user in user_results
        if isinstance(user.get("accountId"), str) and str(user.get("accountId")).strip()
    ]
    return _dedupe_non_empty(groups), _dedupe_non_empty(users)


def parse_group_identifier_fields(raw: str) -> tuple[str, ...]:
    """환경 변수 문자열을 group identifier field 우선순위 tuple로 변환한다.

    빈 값 또는 구분자만 있는 값은 운영 실수를 조기에 드러내기 위해 ValueError로 거부한다.
    """
    fields = tuple(field.strip() for field in raw.split(",") if field.strip())
    if not fields:
        raise ValueError("atlassian_group_acl_field_order must contain at least one field")
    return fields


def _ancestor_chain(page_id: str, parent_by_id: dict[str, str | None]) -> list[str]:
    """크롤 payload 의 parent_id 맵으로 조상 id 체인을 만든다 — 가까운 조상 → 루트 순.

    같은 크롤 배치에 없는 parent(루트 밖/권한 밖)는 체인에 포함하되 그 위로는 더
    걷지 않는다. 사이클·과도 깊이는 방어적으로 중단한다(A2 후속 — 2026-06-10).
    """
    chain: list[str] = []
    visited = {page_id}
    current = page_id
    for _ in range(50):
        parent = parent_by_id.get(current)
        if not parent or parent in visited:
            break
        chain.append(parent)
        visited.add(parent)
        current = parent
    return chain


def parse_empty_restriction_policy(raw: str) -> str:
    """page-level restriction empty 처리 정책 문자열을 검증한다."""
    policy = raw.strip()
    if policy not in EMPTY_RESTRICTION_POLICIES:
        allowed = ", ".join(sorted(EMPTY_RESTRICTION_POLICIES))
        raise ValueError(
            f"atlassian_empty_restriction_policy must be one of {allowed}; got {raw!r}"
        )
    return policy


def _restriction_results(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    results = value.get("results")
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict)]


def _group_acl_value(group: dict[str, Any], identifier_fields: tuple[str, ...]) -> str:
    for key in identifier_fields:
        value = group.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _with_prefix(value: str, prefix: str) -> str:
    if not value:
        return ""
    return f"{prefix}{value}" if prefix else value


def _dedupe_non_empty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(normalized)
    return deduped


def synthesize_space_acl(space_key: str) -> tuple[list[str], list[str]]:
    """PoC ACL 합성(space_key 기반) — Full Crawl·Delta Sync 어댑터가 공유한다.

    에이전트 MVP 가 ACL 을 산출하지 않으므로 ``["space:{space_key}"]`` 그룹으로 합성한다.
    운영 ACL 연동 시 Admin Key + read restriction 조회 결과로 본 함수 호출부를 교체한다
    (RAG 검색 ACL 필터와 공유 계약).
    """
    return [f"space:{space_key}"], []


def synthesize_authenticated_acl(public_acl_group: str) -> tuple[list[str], list[str]]:
    """restriction 없는 페이지에 부여하는 "모든 인증 사용자 허용" sentinel ACL 을 합성한다.

    page-level read restriction 이 비어 있는 페이지(=조직 내 인증 사용자 누구나 열람
    가능한 페이지)에 ``[public_acl_group]`` 그룹을 부여한다. 빈 ACL 로 두면 색인 단계
    ACL gate(``INVALID_ACL``)가 차단하므로, 모든 인증 사용자에게 열려야 하는 페이지를
    색인 가능 상태로 만든다.

    이 sentinel 이 실제 검색 허용으로 이어지려면 RAG 검색 측 ``build_acl_filter`` 가
    동일 토큰을 모든 principal 의 그룹에 주입해야 한다(ingestion↔rag 공유 계약 —
    docs/db-schema.md §1.4, ADR 0003).

    Args:
        public_acl_group: sentinel group 토큰(예: ``"*"``). 빈 값은 운영 실수를
            조기에 드러내기 위해 ValueError 로 거부한다.

    Returns:
        ``([public_acl_group], [])`` — sentinel 그룹 1개, 사용자 목록은 비어 있음.
    """
    token = public_acl_group.strip()
    if not token:
        raise ValueError("atlassian_public_acl_group must be a non-empty token")
    return [token], []


def _default_workflow_runner() -> _WorkflowRunner:
    """기본 workflow runner — vendored full crawl workflow 를 지연 import 한다.

    지연 import 로 vendored(StrEnum, Python 3.11) 의존을 import 시점이 아닌 실행 시점으로
    미룬다(app 패키지가 vendored 미설치 환경에서도 import 가능하도록).
    """
    from data_ingestion_agent.workflow import run_full_crawl_workflow

    runner: _WorkflowRunner = run_full_crawl_workflow
    return runner


def _default_confluence_client(
    *,
    cloud_id: str,
    access_token: str,
    request_delay_seconds: float,
    max_retries: int,
    timeout_seconds: int,
    use_admin_key: bool,
) -> Any:
    from data_ingestion_agent.config import DataIngestionConfig
    from data_ingestion_agent.confluence import ConfluenceClient

    config = DataIngestionConfig(
        cloud_id=cloud_id,
        access_token=access_token,
        output_dir=tempfile.gettempdir(),
        request_delay_seconds=request_delay_seconds,
        max_retries=max_retries,
        timeout_seconds=timeout_seconds,
        use_admin_key=use_admin_key,
    )
    return ConfluenceClient(config=config)


def _parse_last_modified(value: str) -> datetime:
    """에이전트 ``last_modified_at``(ISO 8601) → datetime.

    빈 문자열은 매핑 실패로 간주해 명시적으로 거부한다(epoch 등으로 무음 보정하면 Delta
    Sync 비교가 오염되므로). 정상 에이전트 산출물은 version.createdAt 으로 항상 채워진다.
    """
    if not value:
        raise ValueError("last_modified_at is required for PageObject mapping")
    return parse_atlassian_datetime(value)
