"""apply_soft_deletes 단위 테스트 — 3중 삭제 트리거 공통 funnel (featureI-5b).

작성자 : 최태성
담당 영역 : ingestion

dedup·정렬 결정론, id 단위 예외 격리(부분 성공), 빈 입력 no-op, page/attachment 혼합을 검증한다.
외부 의존성은 호출을 기록하는 fake SoftDeleteStore 로 대체한다.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.ingestion.soft_delete import SoftDeleteResult, apply_soft_deletes


class _RecordingStore:
    """SoftDeleteStore 시그니처 fake — 호출 id 를 기록하고, 지정 id 는 예외를 던진다."""

    def __init__(
        self,
        *,
        fail_page_ids: Iterable[str] = (),
        fail_attachment_ids: Iterable[str] = (),
    ) -> None:
        self.page_calls: list[str] = []
        self.attachment_calls: list[str] = []
        self._fail_pages = set(fail_page_ids)
        self._fail_attachments = set(fail_attachment_ids)

    def soft_delete_by_page_id(self, page_id: str) -> None:
        self.page_calls.append(page_id)
        if page_id in self._fail_pages:
            raise RuntimeError(f"qdrant set_payload failed: {page_id}")

    def soft_delete_by_attachment_id(self, attachment_id: str) -> None:
        self.attachment_calls.append(attachment_id)
        if attachment_id in self._fail_attachments:
            raise RuntimeError(f"qdrant set_payload failed: {attachment_id}")


def test_apply_soft_deletes_pages_and_attachments() -> None:
    store = _RecordingStore()

    result = apply_soft_deletes(
        store=store,
        page_ids=["P2", "P1"],
        attachment_ids=["A1"],
    )

    # 결정론: 정렬된 순서로 store 호출 + 결과.
    assert store.page_calls == ["P1", "P2"]
    assert store.attachment_calls == ["A1"]
    assert result.soft_deleted_page_ids == ["P1", "P2"]
    assert result.soft_deleted_attachment_ids == ["A1"]
    assert result.total_soft_deleted == 3
    assert result.has_failures is False


def test_apply_soft_deletes_normalizes_dedup_whitespace_and_empty() -> None:
    store = _RecordingStore()

    result = apply_soft_deletes(
        store=store,
        page_ids=["P1", "P1", "  P2 ", "", "   "],
    )

    # 중복 제거 + strip + falsy 제외 → store 는 정규화된 고유 id 만 1회씩 받는다.
    assert store.page_calls == ["P1", "P2"]
    assert result.soft_deleted_page_ids == ["P1", "P2"]
    assert result.soft_deleted_attachment_ids == []


def test_apply_soft_deletes_isolates_per_id_failure() -> None:
    # P2 page, A2 attachment 에서 store 가 예외 → 격리되어 나머지는 계속 진행.
    store = _RecordingStore(fail_page_ids=["P2"], fail_attachment_ids=["A2"])

    result = apply_soft_deletes(
        store=store,
        page_ids=["P1", "P2", "P3"],
        attachment_ids=["A1", "A2"],
    )

    # 모든 id 에 대해 store 호출은 시도된다(중단 없음).
    assert store.page_calls == ["P1", "P2", "P3"]
    assert store.attachment_calls == ["A1", "A2"]
    # 성공/실패 분리 집계.
    assert result.soft_deleted_page_ids == ["P1", "P3"]
    assert result.failed_page_ids == ["P2"]
    assert result.soft_deleted_attachment_ids == ["A1"]
    assert result.failed_attachment_ids == ["A2"]
    assert result.has_failures is True
    assert result.total_soft_deleted == 3


def test_apply_soft_deletes_empty_input_is_noop() -> None:
    store = _RecordingStore()

    result = apply_soft_deletes(store=store)

    assert store.page_calls == []
    assert store.attachment_calls == []
    assert result == SoftDeleteResult()
    assert result.total_soft_deleted == 0
    assert result.has_failures is False
