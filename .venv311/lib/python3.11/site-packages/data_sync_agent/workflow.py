from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Sync Agent delta sync workflow orchestration 구현.
          기존 snapshot repository, Confluence client, diff engine, changed page
          processor, deleted/message payload helper를 연결하고 LangGraph 미설치
          환경에서는 sequential fallback으로 실행한다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature7 workflow 및 local output orchestration 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - LangGraph optional dependency, 미설치 시 sequential fallback
--------------------------------------------------
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from data_sync_agent.config import DataSyncConfig
from data_sync_agent.confluence import (
    ConfluenceMetadataClient,
    map_page_metadata_to_snapshot_item,
)
from data_sync_agent.messaging import (
    LocalMessagePayloadWriter,
    build_deleted_item_from_change,
    build_message_payloads,
)
from data_sync_agent.schemas import (
    ChangedDocument,
    DeletedItem,
    FailedItem,
    FailedItemStage,
    FailedItemType,
    MessagePayload,
    PageSnapshot,
    PageSnapshotItem,
    SyncReport,
    SyncReportCounts,
    SyncReportStatus,
)
from data_sync_agent.sync import (
    ChangedPageProcessor,
    DiffResult,
    LocalSnapshotRepository,
    SnapshotRepository,
    diff_snapshots,
)


class DataSyncClient(Protocol):
    """Workflow가 필요로 하는 Confluence client protocol."""

    def list_spaces(self) -> list[dict[str, Any]]:
        """Confluence Space 목록을 반환한다."""

    def list_space_pages(self, space_id: str) -> list[dict[str, Any]]:
        """Space별 Page metadata 목록을 반환한다."""

    def get_page_detail(self, page_id: str) -> dict[str, Any]:
        """Page detail을 반환한다."""


@dataclass(slots=True)
class DataSyncWorkflowState:
    """Data Sync workflow node 간 공유 state."""

    config: DataSyncConfig
    client: DataSyncClient
    snapshot_repository: SnapshotRepository
    sync_id: str
    generated_at: str
    previous_snapshot: PageSnapshot | None = None
    spaces: list[dict[str, Any]] = field(default_factory=list)
    current_pages: list[PageSnapshotItem] = field(default_factory=list)
    current_snapshot: PageSnapshot | None = None
    diff_result: DiffResult | None = None
    changed_documents: list[ChangedDocument] = field(default_factory=list)
    deleted_items: list[DeletedItem] = field(default_factory=list)
    message_payloads: list[MessagePayload] = field(default_factory=list)
    failed_items: list[FailedItem] = field(default_factory=list)
    report: SyncReport | None = None
    output_paths: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DataSyncWorkflowResult:
    """Data Sync workflow 실행 결과."""

    sync_id: str
    changed_documents: list[ChangedDocument]
    deleted_items: list[DeletedItem]
    message_payloads: list[MessagePayload]
    failed_items: list[FailedItem]
    report: SyncReport
    output_paths: dict[str, str]
    uses_langgraph: bool
    mode: str


@dataclass(frozen=True, slots=True)
class DataSyncWorkflow:
    """Optional LangGraph wrapper와 sequential fallback 정보를 담는 workflow handle."""

    uses_langgraph: bool
    mode: str

    def run(self, state: DataSyncWorkflowState) -> DataSyncWorkflowState:
        """구성된 workflow를 실행한다."""
        return _run_sequential_nodes(state)


def build_data_sync_workflow(*, force_sequential: bool = False) -> DataSyncWorkflow:
    """LangGraph 사용 가능 여부에 따라 workflow handle을 생성한다."""
    if force_sequential:
        return DataSyncWorkflow(uses_langgraph=False, mode="sequential_fallback")
    try:
        __import__("langgraph.graph")
    except ModuleNotFoundError:
        return DataSyncWorkflow(uses_langgraph=False, mode="sequential_fallback")
    return DataSyncWorkflow(uses_langgraph=True, mode="langgraph_optional")


def run_data_sync_workflow(
    *,
    config: DataSyncConfig,
    client: DataSyncClient | None = None,
    snapshot_repository: SnapshotRepository | None = None,
    now: Callable[[], str] | None = None,
    force_sequential: bool = False,
) -> DataSyncWorkflowResult:
    """Data Sync Agent delta sync workflow를 실행한다."""
    config.validate()
    generated_at = now() if now is not None else _utc_now_iso()
    sync_id = f"sync-{_safe_timestamp(generated_at)}"
    resolved_client = client or ConfluenceMetadataClient(config=config)
    resolved_repository = snapshot_repository or LocalSnapshotRepository(config.output_dir)
    workflow = build_data_sync_workflow(force_sequential=force_sequential)
    state = DataSyncWorkflowState(
        config=config,
        client=resolved_client,
        snapshot_repository=resolved_repository,
        sync_id=sync_id,
        generated_at=generated_at,
    )

    final_state = workflow.run(state)
    if final_state.report is None:
        raise RuntimeError("workflow completed without report")
    return DataSyncWorkflowResult(
        sync_id=final_state.sync_id,
        changed_documents=final_state.changed_documents,
        deleted_items=final_state.deleted_items,
        message_payloads=final_state.message_payloads,
        failed_items=final_state.failed_items,
        report=final_state.report,
        output_paths=final_state.output_paths,
        uses_langgraph=workflow.uses_langgraph,
        mode=workflow.mode,
    )


def _run_sequential_nodes(state: DataSyncWorkflowState) -> DataSyncWorkflowState:
    for node in (
        load_config,
        load_previous_snapshot,
        list_spaces,
        fetch_current_page_metadata,
        build_current_snapshot,
        diff_previous_current_snapshots,
        fetch_changed_page_details,
        transform_changed_html,
        build_changed_documents,
        build_deleted_items,
        build_message_payload_nodes,
        write_outputs,
        write_report,
    ):
        state = node(state)
    return state


def load_config(state: DataSyncWorkflowState) -> DataSyncWorkflowState:
    """Config validation node."""
    state.config.validate()
    return state


def load_previous_snapshot(state: DataSyncWorkflowState) -> DataSyncWorkflowState:
    """Previous snapshot load node."""
    try:
        state.previous_snapshot = state.snapshot_repository.load_previous_snapshot(
            state.config.previous_snapshot,
            cloud_id=state.config.cloud_id,
            sync_id=state.sync_id,
            generated_at=state.generated_at,
        )
    except Exception as exc:
        state.failed_items.append(
            _failed_item(
                state,
                stage=FailedItemStage.LOAD_PREVIOUS_SNAPSHOT,
                item_type=FailedItemType.SNAPSHOT,
                item_id=str(state.config.previous_snapshot),
                exc=exc,
                retryable=False,
            )
        )
        state.previous_snapshot = PageSnapshot(
            snapshot_id=f"empty-previous-{state.sync_id}",
            sync_id=state.sync_id,
            cloud_id=state.config.cloud_id,
            created_at=state.generated_at,
            pages=[],
        )
    return state


def list_spaces(state: DataSyncWorkflowState) -> DataSyncWorkflowState:
    """Space list collection node."""
    try:
        state.spaces = state.client.list_spaces()
    except Exception as exc:
        state.failed_items.append(
            _failed_item(
                state,
                stage=FailedItemStage.LIST_SPACES,
                item_type=FailedItemType.SYNC_JOB,
                item_id=None,
                exc=exc,
                retryable=_is_retryable(exc),
            )
        )
        state.spaces = []
    return state


def fetch_current_page_metadata(state: DataSyncWorkflowState) -> DataSyncWorkflowState:
    """Space별 Page metadata 수집 node."""
    current_pages: list[PageSnapshotItem] = []
    for space in state.spaces:
        space_id = str(space.get("id") or "")
        if not space_id:
            state.failed_items.append(
                _failed_item_from_message(
                    state,
                    stage=FailedItemStage.FETCH_PAGE_METADATA,
                    item_type=FailedItemType.SPACE,
                    item_id=None,
                    error_type="invalid_space",
                    error_message="space id is required",
                    retryable=False,
                )
            )
            continue
        try:
            page_payloads = state.client.list_space_pages(space_id)
            current_pages.extend(
                map_page_metadata_to_snapshot_item(
                    page_payload,
                    space=space,
                    cloud_id=state.config.cloud_id,
                )
                for page_payload in page_payloads
            )
        except Exception as exc:
            state.failed_items.append(
                _failed_item(
                    state,
                    stage=FailedItemStage.FETCH_PAGE_METADATA,
                    item_type=FailedItemType.SPACE,
                    item_id=space_id,
                    exc=exc,
                    retryable=_is_retryable(exc),
                )
            )
    state.current_pages = current_pages
    return state


def build_current_snapshot(state: DataSyncWorkflowState) -> DataSyncWorkflowState:
    """Current snapshot build node."""
    state.current_snapshot = PageSnapshot(
        snapshot_id=f"current-{state.sync_id}",
        sync_id=state.sync_id,
        cloud_id=state.config.cloud_id,
        created_at=state.generated_at,
        pages=state.current_pages,
    )
    return state


def _unavailable_page_keys(state: DataSyncWorkflowState) -> set[str]:
    """이번 sync 에서 메타데이터를 신뢰성 있게 확인하지 못한 '이전 snapshot' 페이지의 page_key.

    Space 목록(list_spaces) 또는 특정 space 의 페이지 목록(fetch_page_metadata) 조회가
    실패하면 그 space 의 페이지가 current snapshot 에서 통째로 누락된다. 이를 그대로 diff
    하면 일시적 장애(타임아웃·5xx)가 '대량 삭제'로 오인되어 삭제 메시지가 발행된다. 실패한
    범위에 속하는 이전 페이지의 page_key 를 모아 diff 의 ``unavailable_page_keys`` 로 넘기면
    그 페이지들은 DELETED_CANDIDATE 대신 FAILED 로 분류되어 삭제에서 보호된다(데이터 무결성).

    분류 기준:
      - LIST_SPACES 실패: space 자체를 열거하지 못했으므로 이전 페이지 '전체'를 보호한다.
      - FETCH_PAGE_METADATA(SPACE) 실패: 해당 space_id 의 이전 페이지만 보호한다.

    정상적으로 조회된 space 에서 실제로 사라진 페이지는 여전히 DELETED_CANDIDATE 로 남는다
    (정상 삭제 탐지는 무영향).
    """
    previous = state.previous_snapshot
    if previous is None:
        return set()

    if any(item.stage == FailedItemStage.LIST_SPACES for item in state.failed_items):
        return {str(page.page_key) for page in previous.pages}

    failed_space_ids = {
        item.item_id
        for item in state.failed_items
        if item.stage == FailedItemStage.FETCH_PAGE_METADATA
        and item.item_type == FailedItemType.SPACE
        and item.item_id
    }
    if not failed_space_ids:
        return set()
    return {
        str(page.page_key) for page in previous.pages if page.space_id in failed_space_ids
    }


def diff_previous_current_snapshots(
    state: DataSyncWorkflowState,
) -> DataSyncWorkflowState:
    """Snapshot diff node."""
    if state.previous_snapshot is None or state.current_snapshot is None:
        raise RuntimeError("previous and current snapshots are required")
    try:
        state.diff_result = diff_snapshots(
            state.previous_snapshot,
            state.current_snapshot,
            unavailable_page_keys=_unavailable_page_keys(state),
        )
    except Exception as exc:
        state.failed_items.append(
            _failed_item(
                state,
                stage=FailedItemStage.DIFF_SNAPSHOTS,
                item_type=FailedItemType.SYNC_JOB,
                item_id=None,
                exc=exc,
                retryable=False,
            )
        )
        raise
    return state


def fetch_changed_page_details(state: DataSyncWorkflowState) -> DataSyncWorkflowState:
    """Changed page detail fetch와 changed document build를 기존 processor에 위임한다."""
    if state.diff_result is None:
        raise RuntimeError("diff_result is required")
    processing_result = ChangedPageProcessor(client=state.client).process(
        state.diff_result.changed_pages,
        sync_id=state.sync_id,
        cloud_id=state.config.cloud_id,
        detected_at=state.generated_at,
    )
    state.changed_documents = processing_result.changed_documents
    state.failed_items.extend(processing_result.failed_items)
    return state


def transform_changed_html(state: DataSyncWorkflowState) -> DataSyncWorkflowState:
    """Changed HTML transform node.

    HTML 변환은 ChangedPageProcessor 내부 service가 이미 수행하므로 orchestration
    node에서는 명시적 단계만 유지한다.
    """
    return state


def build_changed_documents(state: DataSyncWorkflowState) -> DataSyncWorkflowState:
    """Changed document build node.

    Changed document 생성도 ChangedPageProcessor가 담당하므로 node contract만 유지한다.
    """
    return state


def build_deleted_items(state: DataSyncWorkflowState) -> DataSyncWorkflowState:
    """Deleted candidate diff item을 DeletedItem 산출물로 변환한다."""
    if state.diff_result is None:
        raise RuntimeError("diff_result is required")
    state.deleted_items = [
        build_deleted_item_from_change(
            page_change,
            sync_id=state.sync_id,
            detected_at=state.generated_at,
        )
        for page_change in state.diff_result.deleted_candidates
    ]
    return state


def build_message_payload_nodes(state: DataSyncWorkflowState) -> DataSyncWorkflowState:
    """Changed/deleted 산출물을 downstream message payload로 변환한다."""
    if state.diff_result is None:
        raise RuntimeError("diff_result is required")
    state.message_payloads = build_message_payloads(
        changed_documents=state.changed_documents,
        deleted_items=state.deleted_items,
        skipped_changes=state.diff_result.unchanged_pages + state.diff_result.failed_pages,
    )
    return state


def write_outputs(state: DataSyncWorkflowState) -> DataSyncWorkflowState:
    """Current snapshot, changed/deleted/message/failed local output을 저장한다."""
    if state.current_snapshot is None:
        raise RuntimeError("current_snapshot is required")
    repository = state.snapshot_repository
    snapshot_result = repository.save_current_snapshot(
        state.current_snapshot,
        generated_at=state.generated_at,
    )
    state.output_paths["current_snapshot"] = str(snapshot_result.path)
    state.output_paths["changed_documents"] = str(
        _write_jsonl(
            state.config.output_dir / "changed" / "changed_documents.jsonl",
            (document.to_dict() for document in state.changed_documents),
        )
    )
    state.output_paths["deleted_items"] = str(
        _write_jsonl(
            state.config.output_dir / "deleted" / "deleted_items.jsonl",
            (deleted_item.to_dict() for deleted_item in state.deleted_items),
        )
    )
    message_path = LocalMessagePayloadWriter(state.config.output_dir).write(
        state.message_payloads
    )
    state.output_paths["message_payloads"] = str(message_path)
    state.output_paths["failed_items"] = str(
        _write_jsonl(
            state.config.output_dir / "failed" / "failed_items.jsonl",
            (failed_item.to_dict() for failed_item in state.failed_items),
        )
    )
    return state


def write_report(state: DataSyncWorkflowState) -> DataSyncWorkflowState:
    """Sync report를 생성하고 local JSON 파일로 저장한다."""
    if state.diff_result is None:
        raise RuntimeError("diff_result is required")
    counts = SyncReportCounts(
        spaces=len(state.spaces),
        pages_seen=len(state.current_pages),
        new_pages=state.diff_result.summary.new_pages,
        updated_pages=state.diff_result.summary.updated_pages,
        unchanged_pages=state.diff_result.summary.unchanged_pages,
        deleted_candidates=state.diff_result.summary.deleted_candidates,
        failed_items=len(state.failed_items),
    )
    status = (
        SyncReportStatus.COMPLETED_WITH_ERRORS
        if state.failed_items
        else SyncReportStatus.COMPLETED
    )
    report = SyncReport(
        sync_id=state.sync_id,
        status=status,
        counts=counts,
        output_paths=dict(state.output_paths),
    )
    report_path = state.config.output_dir / "reports" / "sync_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    state.output_paths["report"] = str(report_path)
    state.report = SyncReport(
        sync_id=state.sync_id,
        status=status,
        counts=counts,
        output_paths=dict(state.output_paths),
    )
    report_path.write_text(
        json.dumps(state.report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return state


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def _failed_item(
    state: DataSyncWorkflowState,
    *,
    stage: FailedItemStage,
    item_type: FailedItemType,
    item_id: str | None,
    exc: Exception,
    retryable: bool,
) -> FailedItem:
    return _failed_item_from_message(
        state,
        stage=stage,
        item_type=item_type,
        item_id=item_id,
        error_type=getattr(exc, "error_type", type(exc).__name__),
        error_message=_safe_error_message(str(exc), state.config.access_token),
        retryable=retryable,
        attempt_count=int(getattr(exc, "attempt_count", 1)),
    )


def _failed_item_from_message(
    state: DataSyncWorkflowState,
    *,
    stage: FailedItemStage,
    item_type: FailedItemType,
    item_id: str | None,
    error_type: str,
    error_message: str,
    retryable: bool,
    attempt_count: int = 1,
) -> FailedItem:
    return FailedItem(
        sync_id=state.sync_id,
        stage=stage,
        item_type=item_type,
        item_id=item_id,
        error_type=error_type,
        error_message=_safe_error_message(error_message, state.config.access_token),
        retryable=retryable,
        attempt_count=attempt_count,
    )


def _is_retryable(exc: Exception) -> bool:
    return bool(getattr(exc, "retryable", False))


def _safe_error_message(message: str, access_token: str) -> str:
    return (
        message.replace(access_token, "<redacted>")
        .replace("Authorization", "<redacted-header>")
        .replace("Bearer", "<redacted-auth-scheme>")
        .replace("access_token", "<redacted-token-field>")
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _safe_timestamp(value: str) -> str:
    return (
        value.replace(":", "")
        .replace("-", "")
        .replace(".", "")
        .replace("T", "T")
        .replace("Z", "Z")
    )
