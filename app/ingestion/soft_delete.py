"""soft-delete 적용 seam — 3중 삭제 트리거 공통 funnel (FR-005 / ADR 0003 항목 4).

--------------------------------------------------
작성자 : 최태성
작성목적 : Delta `deleted_candidate` / Confluence Trash API / 실시간 Webhook 세 삭제 트리거가
          공통으로 호출하는 단일 soft-delete 적용 함수를 제공한다. ADR 0003 항목 4로 도입된
          store soft-delete 능력(`soft_delete_by_page_id`/`soft_delete_by_attachment_id`)을
          호출해 Qdrant payload `is_deleted=true` 를 set 한다(보존 삭제). 입력은 결정론적으로
          정규화하고, id 단위로 예외를 격리해 한 건의 실패가 나머지 삭제를 막지 않게 한다.
작성일 : 2026-06-04 (featureI-5b — 3중 삭제 트리거 배선)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-06-04, 최초 작성, featureI-5b — SoftDeleteStore Protocol + SoftDeleteResult +
    apply_soft_deletes(dedup·정렬·id별 격리). Sync Worker(Delta/Trash/Webhook)가 본 함수로 수렴.
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - 외부 의존성 0 (주입된 SoftDeleteStore 가 외부 의존성을 갖는다)
--------------------------------------------------
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol


class SoftDeleteStore(Protocol):
    """soft-delete 능력만 노출하는 store seam (ADR 0003 항목 4).

    실 구현은 ``QdrantPoolStore``, 테스트는 ``FakeQdrantPoolStore`` — 둘 다 동일 시그니처를
    이미 갖는다. 본 Protocol 로 funnel/Worker 가 전체 store 계약이 아니라 soft-delete 2개
    메서드에만 의존하게 한다(결합 최소화).
    """

    def soft_delete_by_page_id(self, page_id: str) -> None:
        """``page_id`` 일치 청크의 payload ``is_deleted`` 를 True 로 set 한다."""
        ...

    def soft_delete_by_attachment_id(self, attachment_id: str) -> None:
        """``attachment_id`` 일치 청크의 payload ``is_deleted`` 를 True 로 set 한다."""
        ...


@dataclass(frozen=True, slots=True)
class SoftDeleteResult:
    """soft-delete 적용 결과 — 트리거(Delta/Trash/Webhook)·잡 기록 공통 리포트.

    Attributes:
        soft_deleted_page_ids: ``is_deleted=true`` 로 set 된 page_id(정렬·중복 제거).
        soft_deleted_attachment_ids: 동일, attachment_id.
        failed_page_ids: store 호출이 예외로 실패한 page_id(격리 — 전체 중단 안 함).
        failed_attachment_ids: 동일, attachment_id.
    """

    soft_deleted_page_ids: list[str] = field(default_factory=list)
    soft_deleted_attachment_ids: list[str] = field(default_factory=list)
    failed_page_ids: list[str] = field(default_factory=list)
    failed_attachment_ids: list[str] = field(default_factory=list)

    @property
    def total_soft_deleted(self) -> int:
        """성공적으로 soft-delete 된 page+attachment 총 개수."""
        return len(self.soft_deleted_page_ids) + len(self.soft_deleted_attachment_ids)

    @property
    def has_failures(self) -> bool:
        """격리된 실패가 하나라도 있으면 True."""
        return bool(self.failed_page_ids or self.failed_attachment_ids)


def _normalize_ids(ids: Iterable[str]) -> list[str]:
    """공백 strip + falsy 제외 + 중복 제거 + 정렬 — 결정론적 처리 보장."""
    return sorted({str(raw).strip() for raw in ids if str(raw).strip()})


def apply_soft_deletes(
    *,
    store: SoftDeleteStore,
    page_ids: Iterable[str] = (),
    attachment_ids: Iterable[str] = (),
) -> SoftDeleteResult:
    """page_id/attachment_id 목록을 store soft-delete 로 적용한다(3 트리거 공통 funnel).

    Delta ``deleted_candidate`` / Confluence Trash API / 실시간 Webhook 세 경로가 모두 이
    함수로 수렴한다. 입력은 dedup+정렬해 결정론적으로 처리하고, **id 단위로 예외를 격리**해
    한 건의 store 실패가 나머지 삭제를 막지 않게 한다(부분 성공 + 실패 목록 surface).

    Args:
        store: soft-delete seam(``soft_delete_by_page_id``/``soft_delete_by_attachment_id``).
        page_ids: soft-delete 대상 page_id(빈 값·중복 허용 — 내부 정규화).
        attachment_ids: soft-delete 대상 attachment_id.

    Returns:
        성공/실패 id 를 분리 집계한 ``SoftDeleteResult``.
    """
    soft_pages: list[str] = []
    failed_pages: list[str] = []
    for page_id in _normalize_ids(page_ids):
        try:
            store.soft_delete_by_page_id(page_id)
        except Exception:  # noqa: BLE001 — store 호출 실패를 id 단위로 격리(전체 중단 금지)
            failed_pages.append(page_id)
        else:
            soft_pages.append(page_id)

    soft_attachments: list[str] = []
    failed_attachments: list[str] = []
    for attachment_id in _normalize_ids(attachment_ids):
        try:
            store.soft_delete_by_attachment_id(attachment_id)
        except Exception:  # noqa: BLE001 — 동일 격리(부분 성공 보장)
            failed_attachments.append(attachment_id)
        else:
            soft_attachments.append(attachment_id)

    return SoftDeleteResult(
        soft_deleted_page_ids=soft_pages,
        soft_deleted_attachment_ids=soft_attachments,
        failed_page_ids=failed_pages,
        failed_attachment_ids=failed_attachments,
    )
