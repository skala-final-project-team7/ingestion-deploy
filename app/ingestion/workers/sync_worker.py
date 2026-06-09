"""Sync Worker — 3중 삭제 트리거 오케스트레이션 (FR-005 / featureI-5b).

--------------------------------------------------
작성자 : 최태성
작성목적 : Qdrant store 를 소유하고, 3중 삭제 동기화의 3개 트리거를 ``apply_soft_deletes``
          단일 funnel 로 모은다.
            1) Delta Sync ``deleted_candidate`` — ``apply_delta_deletions``(확인 게이트 보존:
               기본 미적용, ``confirm=True`` 일 때만 soft_delete).
            2) Confluence Trash API — ``run_trash_sync``(주1회 → 1시간 안전망).
            3) 실시간 Webhook — ``handle_webhook_event``(즉시 삭제 반영).
          Reconciliation(주1회 ghost, hard delete — ``sync.reconcile_deletions``)은 본 Worker
          밖에서 무수정 유지된다(직교).
작성일 : 2026-06-04 (featureI-5b — 3중 삭제 트리거 배선)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-06-04, 최초 작성, featureI-5b — WebhookDeleteEvent / SyncWorkerDeps / SyncWorker
    (apply_delta_deletions·run_trash_sync·handle_webhook_event). 모두 apply_soft_deletes 수렴.
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - 외부 의존성 0 (주입된 SoftDeleteStore / TrashSource 가 외부 의존성을 갖는다)
--------------------------------------------------
"""

from __future__ import annotations

from dataclasses import dataclass

from app.adapters.confluence_trash import TrashSource
from app.ingestion.soft_delete import SoftDeleteResult, SoftDeleteStore, apply_soft_deletes
from app.ingestion.sync import DeltaSyncResult


@dataclass(frozen=True, slots=True)
class WebhookDeleteEvent:
    """Confluence 실시간 삭제 Webhook 1건 — 라우트 파서가 생성, Worker 가 소비한다.

    page 또는 attachment 하나의 삭제를 나타낸다(둘 다 비면 no-op). page 삭제는 본문+첨부가
    cascade 로 함께 색인 제외돼야 하므로, attachment_id 가 없으면 page_id 만으로 처리한다.
    """

    page_id: str | None = None
    attachment_id: str | None = None

    @property
    def is_empty(self) -> bool:
        """삭제 대상 id 가 하나도 없으면 True(no-op 판정)."""
        return not self.page_id and not self.attachment_id


@dataclass
class SyncWorkerDeps:
    """Sync Worker 의존성 — store(필수) + trash_source(선택).

    Attributes:
        store: soft-delete seam(``QdrantPoolStore``/``FakeQdrantPoolStore``).
        trash_source: Confluence Trash 소스. None 이면 ``run_trash_sync`` 는 no-op.
    """

    store: SoftDeleteStore
    trash_source: TrashSource | None = None


class SyncWorker:
    """3중 삭제 트리거를 소유 store 의 soft-delete 로 적용하는 Worker.

    모든 경로가 ``apply_soft_deletes`` 단일 funnel 로 수렴해 id 단위 격리·결정론·부분 성공을
    공유한다. store 호출 실패는 funnel 이 격리하므로 Worker 메서드는 예외를 전파하지 않고
    ``SoftDeleteResult``(성공/실패 분리)로 보고한다.
    """

    def __init__(self, deps: SyncWorkerDeps) -> None:
        self._deps = deps

    def apply_delta_deletions(
        self,
        result: DeltaSyncResult,
        *,
        confirm: bool = False,
    ) -> SoftDeleteResult:
        """Delta Sync 삭제 후보를 soft-delete 한다 — **확인 게이트 보존**.

        ``run_delta_sync`` 는 삭제 후보를 *확인 대기*로만 surface 한다(자동 삭제 아님). 본
        메서드도 기본 ``confirm=False`` 면 아무것도 적용하지 않고 빈 결과를 반환해 기존 정책을
        보존한다. 운영에서 후보가 확인되면(``confirm=True``) 후보 page_id 를 soft-delete 한다.

        Args:
            result: ``run_delta_sync`` 가 반환한 ``DeltaSyncResult``.
            confirm: True 일 때만 ``deleted_candidate_page_ids`` 를 실제 soft-delete 한다.

        Returns:
            적용 결과(``confirm=False`` 면 빈 ``SoftDeleteResult``).
        """
        if not confirm:
            return SoftDeleteResult()
        return apply_soft_deletes(
            store=self._deps.store,
            page_ids=result.deleted_candidate_page_ids,
        )

    def run_trash_sync(self) -> SoftDeleteResult:
        """Confluence Trash API 결과(Trashed page/attachment)를 soft-delete 한다.

        ``trash_source`` 가 없으면 no-op(빈 결과). 주기 스케줄러(주1회 → 1시간)가 호출한다.

        Returns:
            soft-delete 적용 결과.
        """
        if self._deps.trash_source is None:
            return SoftDeleteResult()
        trashed = self._deps.trash_source.list_trashed_ids()
        return apply_soft_deletes(
            store=self._deps.store,
            page_ids=trashed.pages,
            attachment_ids=trashed.attachments,
        )

    def handle_webhook_event(self, event: WebhookDeleteEvent) -> SoftDeleteResult:
        """실시간 삭제 Webhook 1건을 즉시 soft-delete 한다(빈 이벤트는 no-op).

        Args:
            event: 라우트 파서가 만든 ``WebhookDeleteEvent``(page_id 또는 attachment_id).

        Returns:
            soft-delete 적용 결과.
        """
        if event.is_empty:
            return SoftDeleteResult()
        page_ids = [event.page_id] if event.page_id else []
        attachment_ids = [event.attachment_id] if event.attachment_id else []
        return apply_soft_deletes(
            store=self._deps.store,
            page_ids=page_ids,
            attachment_ids=attachment_ids,
        )
