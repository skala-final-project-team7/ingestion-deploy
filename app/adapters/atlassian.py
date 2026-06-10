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
      * ``allow_authenticated`` (기본): ``[public_acl_group]`` 부여 → 모든 인증 사용자 허용.
      * ``space_fallback``: ``["space:{space_key}"]`` 합성(공간 단위 ACL).
      * ``mark_missing``: 빈 ACL → 색인 단계 ``INVALID_ACL`` 차단(보수 정책).
--------------------------------------------------
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from app.adapters.base import ActiveIds, ChangeEvent, DocumentSourceAdapter
from app.adapters.json_fixture import parse_atlassian_datetime
from app.schemas.page_object import PageObject

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

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "empty_restriction_policy",
            parse_empty_restriction_policy(self.empty_restriction_policy),
        )

    def get_page_acl(self, *, page_id: str, space_key: str) -> tuple[list[str], list[str]]:
        raw = self.client.get_page_read_restrictions(page_id)
        allowed_groups, allowed_users = parse_read_restrictions_acl(
            raw,
            group_identifier_fields=self.group_identifier_fields,
            group_acl_prefix=self.group_acl_prefix,
        )
        if allowed_groups or allowed_users:
            return allowed_groups, allowed_users
        if self.empty_restriction_policy == "allow_authenticated":
            return synthesize_authenticated_acl(self.public_acl_group)
        if self.empty_restriction_policy == "space_fallback":
            return synthesize_space_acl(space_key)
        return [], []


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
        for document in self._collect_documents():
            page = self._to_page_object(document)
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

    def _to_page_object(self, document: Any) -> PageObject:
        """vendored ProcessedDocument → 표준 PageObject 변환(모든 변환은 어댑터에서).

        매핑(docs/atlassian-api.md 매핑표 + 에이전트 ProcessedDocument 스키마):
            page.page_id            → page_id
            space.space_key         → space_key
            page.title              → title
            body.storage_html       → body_html  (청커가 HTML 파싱)
            page.version_number     → version_number
            page.last_modified_at   → last_modified (ISO 8601 파싱)
            page.page_url           → webui_link
            restriction API         → allowed_groups/allowed_users (Admin Key 경로)
            (admin key off)         → allowed_groups/allowed_users (PoC space 합성)
            (MVP 미산출)            → labels=[] / ancestors=[] / attachments=[]
        """
        space_key = document.space.space_key
        allowed_groups, allowed_users = self._resolve_acl(
            page_id=document.page.page_id, space_key=space_key
        )
        return PageObject(
            page_id=document.page.page_id,
            space_key=space_key,
            title=document.page.title,
            body_html=document.body.storage_html,
            version_number=document.page.version_number,
            last_modified=_parse_last_modified(document.page.last_modified_at),
            allowed_groups=allowed_groups,
            allowed_users=allowed_users,
            webui_link=document.page.page_url,
            labels=[],
            ancestors=[],
            attachments=[],
        )

    def _synthesize_acl(self, space_key: str) -> tuple[list[str], list[str]]:
        """PoC ACL 합성 — space_key 기반 그룹(JsonFixtureSourceAdapter 패턴 동일).

        ``atlassian_use_admin_key`` 가 꺼져 있어 ACL provider 가 없을 때의 폴백이다.
        page-level restriction 이 비어 있는 경우 상위 folder/page/space permission 처리
        정책은 ``ConfluenceRestrictionAclProvider`` 로 옮겼다(docs/db-schema.md §1.4, ADR 0003).
        """
        return synthesize_space_acl(space_key)

    def _resolve_acl(self, *, page_id: str, space_key: str) -> tuple[list[str], list[str]]:
        if self._acl_provider is None:
            return self._synthesize_acl(space_key)
        return self._acl_provider.get_page_acl(page_id=page_id, space_key=space_key)


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
