from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Sync Agent snapshot diff engine 구현.
          previous/current PageSnapshot을 비교해 new/updated/unchanged/deleted_candidate를 분류한다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature4 snapshot diff engine 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses 기반
--------------------------------------------------
"""

from dataclasses import dataclass

from data_sync_agent.schemas import ChangeType, PageSnapshot, PageSnapshotItem


class DiffEngineError(ValueError):
    """Snapshot diff 입력이 유효하지 않은 경우 발생하는 오류."""


@dataclass(frozen=True, slots=True)
class PageChange:
    """Page 단위 diff 분류 결과."""

    change_type: ChangeType
    page_key: str
    previous: PageSnapshotItem | None
    current: PageSnapshotItem | None


@dataclass(frozen=True, slots=True)
class DiffSummary:
    """Snapshot diff count summary."""

    new_pages: int
    updated_pages: int
    unchanged_pages: int
    deleted_candidates: int
    failed_pages: int = 0

    @property
    def changed_pages(self) -> int:
        """후속 상세 조회 대상인 new + updated page 수."""
        return self.new_pages + self.updated_pages


@dataclass(frozen=True, slots=True)
class DiffResult:
    """Snapshot diff result."""

    changed_pages: list[PageChange]
    unchanged_pages: list[PageChange]
    deleted_candidates: list[PageChange]
    failed_pages: list[PageChange]
    summary: DiffSummary


def index_snapshot_pages(
    pages: list[PageSnapshotItem],
    *,
    snapshot_label: str,
) -> dict[str, PageSnapshotItem]:
    """PageSnapshotItem 목록을 page_key 기준 index로 변환한다.

    Duplicate page_key는 silent overwrite 대신 명확한 오류로 처리한다.
    """
    indexed_pages: dict[str, PageSnapshotItem] = {}
    for page in pages:
        page.validate()
        if page.page_key in indexed_pages:
            raise DiffEngineError(
                f"duplicate page_key in {snapshot_label} snapshot: {page.page_key}"
            )
        indexed_pages[str(page.page_key)] = page
    return indexed_pages


def diff_snapshots(
    previous: PageSnapshot,
    current: PageSnapshot,
    *,
    unavailable_page_keys: set[str] | None = None,
) -> DiffResult:
    """previous/current snapshot을 비교해 deterministic diff result를 반환한다."""
    previous.validate()
    current.validate()
    previous_pages = index_snapshot_pages(previous.pages, snapshot_label="previous")
    current_pages = index_snapshot_pages(current.pages, snapshot_label="current")
    unavailable_page_keys = unavailable_page_keys or set()

    changed_pages: list[PageChange] = []
    unchanged_pages: list[PageChange] = []
    deleted_candidates: list[PageChange] = []
    failed_pages: list[PageChange] = []

    for page_key in sorted(current_pages):
        current_page = current_pages[page_key]
        previous_page = previous_pages.get(page_key)
        if previous_page is None:
            changed_pages.append(
                PageChange(
                    change_type=ChangeType.NEW,
                    page_key=page_key,
                    previous=None,
                    current=current_page,
                )
            )
            continue

        if _is_updated(previous_page, current_page):
            changed_pages.append(
                PageChange(
                    change_type=ChangeType.UPDATED,
                    page_key=page_key,
                    previous=previous_page,
                    current=current_page,
                )
            )
        else:
            unchanged_pages.append(
                PageChange(
                    change_type=ChangeType.UNCHANGED,
                    page_key=page_key,
                    previous=previous_page,
                    current=current_page,
                )
            )

    for page_key in sorted(set(previous_pages) - set(current_pages)):
        if page_key in unavailable_page_keys:
            failed_pages.append(
                PageChange(
                    change_type=ChangeType.FAILED,
                    page_key=page_key,
                    previous=previous_pages[page_key],
                    current=None,
                )
            )
            continue
        deleted_candidates.append(
            PageChange(
                change_type=ChangeType.DELETED_CANDIDATE,
                page_key=page_key,
                previous=previous_pages[page_key],
                current=None,
            )
        )

    summary = DiffSummary(
        new_pages=sum(
            1 for change in changed_pages if change.change_type == ChangeType.NEW
        ),
        updated_pages=sum(
            1 for change in changed_pages if change.change_type == ChangeType.UPDATED
        ),
        unchanged_pages=len(unchanged_pages),
        deleted_candidates=len(deleted_candidates),
        failed_pages=len(failed_pages),
    )
    return DiffResult(
        changed_pages=changed_pages,
        unchanged_pages=unchanged_pages,
        deleted_candidates=deleted_candidates,
        failed_pages=failed_pages,
        summary=summary,
    )


def _is_updated(previous: PageSnapshotItem, current: PageSnapshotItem) -> bool:
    return (
        current.version_number != previous.version_number
        or current.last_modified_at != previous.last_modified_at
    )
