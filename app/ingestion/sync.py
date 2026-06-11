"""삭제 동기화 [Pipeline] — Reconciliation (feature6 Phase 3).

--------------------------------------------------
작성자 : 최태성
작성목적 : Delta Sync 가 감지하지 못하는 삭제된 페이지·첨부를 Qdrant 에서 제거하기
          위한 Reconciliation 함수. 설계서 §3.7 Phase 1 흐름 정합 ─ source.
          list_active_ids() 와 Qdrant 적재 ID 의 차집합을 ghost 로 산출해 cascade
          삭제한다. PoC 단계는 본 Reconciliation 만 활성, 운영 전환 시 Trash API
          Sync + Webhook 리스너를 추가해 ‘주 1회 → 1시간 → 즉시’ 3중 안전망으로
          단축한다 (설계서 §3.7).
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature6 Phase 3 — ReconciliationResult 값 객체 +
    reconcile_deletions 함수. 7단계 흐름 정합. jobs 적재·스케줄링·알림은 호출자
    책임으로 분리.
  - 2026-06-10, 코드 리뷰 재점검(A3·A13) — (1) ``run_delta_sync`` 에 ``acl_provider``
    seam 추가: delta 재수집의 ACL 산출을 full crawl 과 통일해 restriction ACL 이
    space 합성 ACL 로 덮이는 over/under-grant 차단. (2) 변경 페이지 루프에 페이지
    단위 격리 추가(crawler.py 정합) — 1건 실패가 delta 잡 전체를 FAILED 로 만들지
    않고 failed_items 로 집계된다.
  - 2026-06-10, A8 잔여 — delta 변환에 space_id/space_name 매핑 추가
    (ChangedDocument.space dict → PageObject — 출처 카드 spaceId/spaceName 원천).
  - 2026-06-11, backend-template 동기화(§2-5 siteUrl — api-spec v2.6.2) — delta 변환의
    ``webui_link`` 도 full crawl 과 동일하게 ``normalize_webui_link`` 로 absolute 정규화
    (``run_delta_sync(site_url=...)`` seam 추가). site_url 은 §2-5 ``siteUrl``
    (=``admin_atlassian_credential.site_url`` 단일 컬럼) 대응 값이다.
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - 외부 의존성 0 (주입된 DocumentSourceAdapter / QdrantPoolStore 가 외부 의존성을
    갖는다)
--------------------------------------------------
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from app.adapters.atlassian import PageAclProvider, normalize_webui_link
from app.adapters.base import DocumentSourceAdapter
from app.adapters.json_fixture import parse_atlassian_datetime
from app.ingestion.crawler import build_chunking_message
from app.ingestion.workers import QUEUE_CHUNKING
from app.ingestion.workers.publisher import QueuePublisher
from app.schemas.page_object import PageObject
from app.storage.raw_store import RawPageStore

if TYPE_CHECKING:
    # 타입 전용 import — 런타임 import 시 app.storage ↔ app.ingestion 순환
    # (storage/__init__ → qdrant_client → app.ingestion/__init__ → sync → qdrant_client)
    # 이 생겨 단독 import 순서에 따라 부분 초기화 오류가 났다(2026-06-10 검증에서 발견).
    from app.storage.qdrant_client import QdrantPoolStore

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Reconciliation 실행 결과 — 호출자(스케줄러·그래프 노드)가 jobs 적재·알림에 사용.

    Attributes:
        deleted_pages: 삭제된 ghost 페이지의 ``page_id`` 목록.
        deleted_attachments: 삭제된 ghost 첨부의 ``attachment_id`` 목록.
    """

    deleted_pages: list[str]
    deleted_attachments: list[str]


def reconcile_deletions(
    *,
    source: DocumentSourceAdapter,
    store: QdrantPoolStore,
) -> ReconciliationResult:
    """source.list_active_ids() 와 Qdrant 적재 ID 의 차집합 ghost 를 cascade 삭제한다.

    설계서 §3.7 Phase 1 흐름 7단계:
        1. ``active_ids = source.list_active_ids()`` — {'pages': set, 'attachments': set}
        2. ``set_B_pages = store.scroll_page_ids()`` — CONTENT_POOL 의 본문 청크 page_id
        3. ``set_B_attaches = store.scroll_attachment_ids()`` — 첨부 청크 attachment_id
        4. ``ghost_pages = set_B_pages - active_ids.pages``
        5. ``ghost_attaches = set_B_attaches - active_ids.attachments``
        6. 각 ghost id 에 대해 ``store.delete_by_page_id`` / ``delete_by_attachment_id``
           호출 — 어댑터가 3 Pool 모두에서 cascade 삭제.
        7. ``ReconciliationResult`` 로 결과 반환 (호출자가 jobs 적재).

    ghost 가 0 이면 delete 호출 자체를 회피한다 — 운영 비용 절감 + false positive
    차단.

    Note:
        설계서 §3.7 의 cascade 모델 — ``set_B_pages`` 는 ``source_type=page`` 청크에서만
        추출되므로, **첨부만 적재되고 본문이 없는 페이지의 ``page_id`` 는 page-level
        ghost 로 잡히지 않는다.** 그 경우 attachment-level scroll 이 별도로
        attachment_id 를 처리한다 (본문 없는 페이지의 첨부는 attachment_id 기준 단독
        reconciliation). 운영에서는 본문 + 첨부가 함께 적재되는 시나리오가 정상이라
        본 모델이 일관성을 깨지 않는다 (설계서 §3.1 + §3.7 정합).

    Args:
        source: 공급원 어댑터. ``list_active_ids`` 만 호출한다.
        store: Qdrant Multi-Pool 저장소. scroll 2종 + delete 2종을 호출한다.

    Returns:
        삭제된 page_id / attachment_id 목록을 담은 ``ReconciliationResult``.
    """
    active_ids = source.list_active_ids()
    stored_page_ids = store.scroll_page_ids()
    stored_attachment_ids = store.scroll_attachment_ids()

    ghost_page_ids = stored_page_ids - active_ids.pages
    ghost_attachment_ids = stored_attachment_ids - active_ids.attachments

    # ghost set 을 정렬해 결정론적 결과를 반환 — 테스트·로깅 안정성.
    deleted_pages = sorted(ghost_page_ids)
    deleted_attachments = sorted(ghost_attachment_ids)

    for page_id in deleted_pages:
        store.delete_by_page_id(page_id)
    for attachment_id in deleted_attachments:
        store.delete_by_attachment_id(attachment_id)

    return ReconciliationResult(
        deleted_pages=deleted_pages,
        deleted_attachments=deleted_attachments,
    )


# ==========================================================================
# Delta Sync 어댑터 [Agent 오케스트레이션] — vendored Data Sync Agent (FR-005)
# --------------------------------------------------------------------------
# featureI-6, 2026-05-26: 저장소 루트에 무수정 vendoring 된 Data Sync Agent
# (``data_sync_agent``)를 in-process 블랙박스로 구동해 Delta Sync 를 잇는다. 변경(new/
# updated) 페이지는 표준 PageObject 로 변환해 raw_pages 적재 + Chunking Queue 재투입
# (FR-001~004 동일 파이프라인), 삭제 후보(deleted_candidate)는 page_id 목록으로 surface
# 한다(확정 삭제 아님 — requires_confirmation). 위 ``reconcile_deletions`` 는 무수정 보존.
#
# 3중 삭제 동기화 중 Reconciliation 은 위 함수, Delta Sync 의 deleted_candidate 감지는
# 아래 어댑터가 담당한다. Trash API / Webhook 은 에이전트 MVP 에는 없고, 본 레포는
# featureI-5b 에서 app/adapters/confluence_trash.py + app/api/webhook_routes.py +
# app/ingestion/workers/sync_worker.py 로 구현했다. 실제 Qdrant soft_delete 실행은
# store 를 소유한 Sync Worker 의 책임이라 본 어댑터는 후보 page_id 만 반환한다(추측 구현 금지).
# ==========================================================================


@dataclass
class DeltaSyncRequest:
    """Delta Sync 트리거 입력(주기 스케줄러 / 수동)."""

    previous_snapshot_path: str
    space_key: str | None = None
    # api-spec v2.5.0 preferred credential lookup key (Data Ingestion Worker → auth-server).
    admin_user_id: str | None = None
    # Legacy PoC: BFF→Ingestion 직접 전달(미확정 TBD). 로그·메시지 페이로드에 남기지 않는다.
    access_token: str | None = None
    cloud_id: str | None = None


@dataclass
class DeltaSyncResult:
    """Delta Sync 잡 결과 리포트."""

    changed_pages: int = 0
    deleted_candidate_page_ids: list[str] = field(default_factory=list)
    failed_items: int = 0
    elapsed_ms: int = 0


class _DeltaSyncWorkflowRunner(Protocol):
    """vendored delta sync workflow 호출 시그니처 — 테스트 주입 지점."""

    def __call__(
        self,
        *,
        config: Any,
        client: Any | None = None,
        snapshot_repository: Any | None = None,
        force_sequential: bool = False,
    ) -> Any:
        """Delta sync workflow 를 실행하고 changed/deleted 결과를 반환한다."""


def run_delta_sync(
    request: DeltaSyncRequest,
    *,
    raw_store: RawPageStore,
    publisher: QueuePublisher,
    client: Any | None = None,
    snapshot_repository: Any | None = None,
    workflow_runner: _DeltaSyncWorkflowRunner | None = None,
    force_sequential: bool = True,
    acl_provider: PageAclProvider | None = None,
    site_url: str = "",
) -> DeltaSyncResult:
    """Delta Sync 실행 (FR-005).

    흐름: 1) vendored Data Sync Agent 워크플로를 in-process 실행(블랙박스) → 2) 변경
    문서(new/updated)를 표준 PageObject 로 변환 → 3) ``raw_pages`` 적재 + Chunking Queue
    재투입 → 4) 삭제 후보 page_id 집계 → 5) ``DeltaSyncResult`` 반환.

    Args:
        request: Delta Sync 트리거 입력(previous snapshot 경로 + 주입 자격증명).
        raw_store: 변경 페이지 ``raw_pages`` 적재 어댑터(테스트는 ``FakeRawPageStore``).
        publisher: Chunking Queue 발행 publisher(테스트는 ``FakeQueuePublisher``).
        client: vendored 에이전트의 Confluence metadata client. None 이면 에이전트가
            운영용 client 를 생성한다. 테스트는 fake client 를 주입한다.
        snapshot_repository: vendored snapshot repository. None 이면 에이전트가 로컬
            파일 repository 를 생성한다.
        workflow_runner: delta sync workflow 호출자. 기본값은 vendored
            ``run_data_sync_workflow``. 테스트에서 교체 가능.
        force_sequential: True(기본)면 LangGraph 미사용 sequential 실행으로 결정론 보장.
        acl_provider: page-level ACL provider seam — **full crawl 과 동일 객체를 주입**해
            delta 재수집이 restriction ACL 을 space 합성 ACL 로 덮어쓰지 않게 한다
            (코드 리뷰 A3). None 이면 PoC space_key 합성 폴백(full crawl 기본과 동일).
        site_url: Confluence base URL(``https://{site}.atlassian.net``) — §2-5 ``siteUrl``
            대응 값. 설정 시 변경 페이지 ``webui_link`` 를 absolute 로 정규화한다
            (full crawl 어댑터와 동일 — ``normalize_webui_link``). 빈 값이면 passthrough.

    Returns:
        변경·삭제 후보 집계를 담은 ``DeltaSyncResult``.

    Note:
        에이전트는 산출물을 로컬 파일로 쓰므로 임시 디렉토리로 우회한다(즉시 정리).
        삭제 후보의 실제 Qdrant soft_delete 는 store 를 소유한 Worker 의 책임이라 본
        함수는 후보 page_id 만 반환한다.
    """
    runner = workflow_runner or _default_delta_workflow_runner()
    # 런 단위 ACL 캐시 초기화 — provider 는 잡 간 재사용되므로(full crawl fetch_pages 와
    # 동일 규칙), 직전 런이 캐시한 restriction 이 이번 delta 의 ACL 산출에 재사용되지
    # 않게 한다(권한 변경 반영).
    reset_cache = getattr(acl_provider, "reset_cache", None)
    if callable(reset_cache):
        reset_cache()
    started = time.monotonic()
    output_dir = tempfile.mkdtemp(prefix="sync-agent-")
    try:
        config = _build_sync_config(request, output_dir=output_dir)
        result = runner(
            config=config,
            client=client,
            snapshot_repository=snapshot_repository,
            force_sequential=force_sequential,
        )
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)

    out = DeltaSyncResult()
    for changed in result.changed_documents:
        if request.space_key and changed.space.get("space_key") != request.space_key:
            continue
        # 페이지 단위 격리(코드 리뷰 A13) — full crawl(crawler.py)과 동일하게 매핑/적재/
        # 발행 실패 1건이 delta 잡 전체를 죽이지 않게 한다. 실패 건은 failed_items 로
        # 집계하고 다음 페이지로 진행한다(부분 발행 잔존 방지).
        try:
            page = _changed_document_to_page_object(
                changed, acl_provider=acl_provider, site_url=site_url
            )
            raw_store.save_page(page)
            publisher.publish(routing_key=QUEUE_CHUNKING, message=build_chunking_message(page))
        except Exception:  # noqa: BLE001 — 페이지 단위 격리(매핑·적재·발행 전 구간)
            _LOGGER.warning(
                "delta sync: failed to process changed page (page_id=%s) — skipping",
                _safe_changed_page_id(changed),
                exc_info=True,
            )
            out.failed_items += 1
            continue
        out.changed_pages += 1

    out.deleted_candidate_page_ids = sorted(item.page_id for item in result.deleted_items)
    out.failed_items += len(result.failed_items)
    out.elapsed_ms = int((time.monotonic() - started) * 1000)
    return out


def _safe_changed_page_id(changed: Any) -> str:
    """로그용 page_id 안전 추출 — 추출 자체가 실패해도 로깅이 깨지지 않게 한다."""
    try:
        return str(changed.page.get("page_id") or "<unknown>")
    except Exception:  # noqa: BLE001 — 로깅 보조 경로
        return "<unknown>"


def _build_sync_config(request: DeltaSyncRequest, *, output_dir: str) -> Any:
    from data_sync_agent.config import DataSyncConfig

    return DataSyncConfig(
        cloud_id=request.cloud_id or "",
        access_token=request.access_token or "",
        output_dir=output_dir,
        previous_snapshot=request.previous_snapshot_path,
    )


def _default_delta_workflow_runner() -> _DeltaSyncWorkflowRunner:
    """기본 delta sync workflow runner — vendored workflow 를 지연 import 한다."""
    from data_sync_agent.workflow import run_data_sync_workflow

    runner: _DeltaSyncWorkflowRunner = run_data_sync_workflow
    return runner


def _changed_document_to_page_object(
    changed: Any, *, acl_provider: PageAclProvider | None = None, site_url: str = ""
) -> PageObject:
    """vendored ChangedDocument(dict 기반 space/page/body) → 표준 PageObject 변환.

    Full Crawl 어댑터와 동일 매핑을 사용한다(공급원 무관 표준 계약). ACL 산출도
    full crawl 과 경로를 통일한다(코드 리뷰 A3) — ``acl_provider`` 가 주입되면
    page-level restriction 기반 ACL(``get_page_acl``)을, 없으면 **빈 ACL(fail-closed)**
    을 사용한다(2026-06-11 — 종전 PoC space_key 합성은 회의 결정으로 제거. ACL 값에
    space key 를 싣는 레거시 폐기, ADR 0002 superseded). raw_pages ``save_page`` 가
    ``$set`` 전체 교체라, 여기서 합성으로 고정하면 delta 1회로 제한 페이지 ACL 이 덮여
    over/under-grant 가 발생한다 — 빈 ACL 은 색인 단계 INVALID_ACL 게이트가 제외한다.
    """
    space = changed.space
    page = changed.page
    body = changed.body
    space_key = str(space.get("space_key") or "")
    page_id = str(page["page_id"])
    if acl_provider is not None:
        allowed_groups, allowed_users = acl_provider.get_page_acl(
            page_id=page_id, space_key=space_key
        )
    else:
        allowed_groups, allowed_users = [], []
    return PageObject(
        page_id=page_id,
        space_key=space_key,
        space_id=str(space.get("space_id") or ""),
        space_name=str(space.get("space_name") or space.get("name") or ""),
        title=str(page.get("title") or ""),
        body_html=str(body.get("storage_html") or ""),
        version_number=int(page.get("version_number") or 0),
        last_modified=_parse_changed_last_modified(str(page.get("last_modified_at") or "")),
        allowed_groups=allowed_groups,
        allowed_users=allowed_users,
        webui_link=normalize_webui_link(str(page.get("page_url") or ""), site_url),
        labels=[],
        ancestors=[],
        attachments=[],
    )


def _parse_changed_last_modified(value: str) -> datetime:
    """ChangedDocument ``last_modified_at``(ISO 8601) → datetime(빈 값은 명시 거부)."""
    if not value:
        raise ValueError("last_modified_at is required for PageObject mapping")
    return parse_atlassian_datetime(value)
