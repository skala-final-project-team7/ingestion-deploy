from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Ingestion Agent full crawl workflow orchestration 구현.
          node는 orchestration만 담당하고 client/mapper/repository service를 재사용한다.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature5 workflow runner 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - LangGraph 미설치 환경에서는 sequential fallback 사용
--------------------------------------------------
"""

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from data_ingestion_agent.config import DataIngestionConfig
from data_ingestion_agent.confluence import ConfluenceApiError, ConfluenceClient
from data_ingestion_agent.ingestion import (
    PageDetailMapper,
    build_failed_item,
    build_ingestion_report,
)
from data_ingestion_agent.schemas import (
    FailedItem,
    FailedItemStage,
    FailedItemType,
    IngestionReport,
    IngestionReportStatus,
    ProcessedDocument,
    SpaceInfo,
)
from data_ingestion_agent.storage import LocalFileRepository, LocalWriteResult


class DataIngestionClient(Protocol):
    """Workflow가 사용하는 Confluence client protocol."""

    def list_spaces(self) -> list[dict[str, Any]]:
        """접근 가능한 Space 목록을 반환한다."""

    def list_page_descendants(self, homepage_id: str) -> list[dict[str, Any]]:
        """Space homepage 기준 descendants page refs를 반환한다."""

    def get_page_detail(self, page_id: str) -> dict[str, Any]:
        """Page 상세 응답을 반환한다."""


@dataclass(slots=True)
class PageRefContext:
    """Page ref와 해당 Space metadata를 함께 보존하는 workflow 내부 구조."""

    space: SpaceInfo
    page_ref: dict[str, Any]


@dataclass(slots=True)
class PageDetailContext:
    """Page detail과 해당 Space/Page ref metadata를 함께 보존한다."""

    space: SpaceInfo
    page_ref: dict[str, Any]
    page_detail: dict[str, Any]


@dataclass(slots=True)
class DataIngestionWorkflowState:
    """Data Ingestion workflow state."""

    config: DataIngestionConfig
    client: DataIngestionClient
    job_id: str = field(default_factory=lambda: f"ingestion-{uuid4().hex}")
    spaces: list[dict[str, Any]] = field(default_factory=list)
    page_refs: list[PageRefContext] = field(default_factory=list)
    page_details: list[PageDetailContext] = field(default_factory=list)
    documents: list[ProcessedDocument] = field(default_factory=list)
    failed_items: list[FailedItem] = field(default_factory=list)
    report: IngestionReport | None = None
    write_result: LocalWriteResult | None = None


@dataclass(frozen=True, slots=True)
class DataIngestionWorkflowResult:
    """Workflow 실행 결과."""

    job_id: str
    documents: list[ProcessedDocument]
    failed_items: list[FailedItem]
    report: IngestionReport
    write_result: LocalWriteResult | None

    def summary(self) -> str:
        """CLI에 출력할 safe summary 문자열을 반환한다."""
        return (
            f"job_id={self.job_id} "
            f"status={self.report.status} "
            f"spaces={self.report.counts.spaces} "
            f"page_refs={self.report.counts.page_refs} "
            f"documents_written={self.report.counts.documents_written} "
            f"failed_items={self.report.counts.failed_items}"
        )


class DataIngestionWorkflowRunner:
    """Full crawl workflow node orchestration runner."""

    def __init__(
        self,
        *,
        config: DataIngestionConfig,
        client: DataIngestionClient | None = None,
        repository: LocalFileRepository | None = None,
        mapper: PageDetailMapper | None = None,
    ) -> None:
        self.config = config
        self.client = client or ConfluenceClient(config=config)
        self.repository = repository or LocalFileRepository(config.output_dir)
        self.mapper = mapper or PageDetailMapper()

    def run(self) -> DataIngestionWorkflowResult:
        """정해진 node 순서로 full crawl workflow를 실행한다."""
        state = DataIngestionWorkflowState(config=self.config, client=self.client)
        for node in (
            self.load_config,
            self.list_spaces,
            self.collect_page_tree,
            self.fetch_page_details,
            self.transform_html,
            self.build_processed_documents,
            self.write_outputs,
            self.write_report,
        ):
            state = node(state)

        if state.report is None:
            raise RuntimeError("workflow completed without report")
        return DataIngestionWorkflowResult(
            job_id=state.job_id,
            documents=state.documents,
            failed_items=state.failed_items,
            report=state.report,
            write_result=state.write_result,
        )

    def load_config(
        self,
        state: DataIngestionWorkflowState,
    ) -> DataIngestionWorkflowState:
        """Config 검증 node."""
        state.config.validate()
        return state

    def list_spaces(
        self,
        state: DataIngestionWorkflowState,
    ) -> DataIngestionWorkflowState:
        """Confluence Space 목록 수집 node."""
        try:
            state.spaces = state.client.list_spaces()
        except Exception as error:
            state.failed_items.append(
                _failed_item_from_error(
                    job_id=state.job_id,
                    stage=FailedItemStage.LIST_SPACES,
                    item_type=FailedItemType.SPACE,
                    item_id=None,
                    error=error,
                )
            )
        return state

    def collect_page_tree(
        self,
        state: DataIngestionWorkflowState,
    ) -> DataIngestionWorkflowState:
        """Space homepageId 기준 descendants Page Tree 수집 node."""
        for space_payload in state.spaces:
            space = _space_info_from_payload(space_payload)
            homepage_id = space_payload.get("homepageId")
            if not homepage_id:
                state.failed_items.append(
                    build_failed_item(
                        job_id=state.job_id,
                        stage=FailedItemStage.COLLECT_PAGE_TREE,
                        item_type=FailedItemType.SPACE,
                        item_id=space.space_id,
                        error_type="missing_homepage_id",
                        error_message="Space homepageId is required for MVP crawl.",
                        retryable=False,
                        attempt_count=1,
                    )
                )
                continue

            try:
                page_refs = state.client.list_page_descendants(str(homepage_id))
            except Exception as error:
                state.failed_items.append(
                    _failed_item_from_error(
                        job_id=state.job_id,
                        stage=FailedItemStage.COLLECT_PAGE_TREE,
                        item_type=FailedItemType.SPACE,
                        item_id=space.space_id,
                        error=error,
                    )
                )
                continue

            state.page_refs.extend(
                PageRefContext(space=space, page_ref=page_ref)
                for page_ref in page_refs
            )
        return state

    def fetch_page_details(
        self,
        state: DataIngestionWorkflowState,
    ) -> DataIngestionWorkflowState:
        """Page 상세 수집 node."""
        for page_ref_context in state.page_refs:
            page_id = str(page_ref_context.page_ref.get("id") or "")
            try:
                page_detail = state.client.get_page_detail(page_id)
            except Exception as error:
                state.failed_items.append(
                    _failed_item_from_error(
                        job_id=state.job_id,
                        stage=FailedItemStage.FETCH_PAGE_DETAIL,
                        item_type=FailedItemType.PAGE,
                        item_id=page_id or None,
                        error=error,
                    )
                )
                continue
            state.page_details.append(
                PageDetailContext(
                    space=page_ref_context.space,
                    page_ref=page_ref_context.page_ref,
                    page_detail=page_detail,
                )
            )
        return state

    def transform_html(
        self,
        state: DataIngestionWorkflowState,
    ) -> DataIngestionWorkflowState:
        """HTML 변환 node.

        실제 변환은 mapper가 feature3 extractor를 호출하므로 이 node는 workflow
        단계 경계를 보존한다.
        """
        return state

    def build_processed_documents(
        self,
        state: DataIngestionWorkflowState,
    ) -> DataIngestionWorkflowState:
        """Page detail context를 ProcessedDocument로 변환하는 node."""
        for page_detail_context in state.page_details:
            page_id = str(page_detail_context.page_ref.get("id") or "")
            try:
                document = self.mapper.to_processed_document(
                    job_id=state.job_id,
                    cloud_id=state.config.cloud_id,
                    space=page_detail_context.space,
                    page_ref=page_detail_context.page_ref,
                    page_detail=page_detail_context.page_detail,
                )
            except Exception as error:
                state.failed_items.append(
                    _failed_item_from_error(
                        job_id=state.job_id,
                        stage=FailedItemStage.TRANSFORM_HTML,
                        item_type=FailedItemType.PAGE,
                        item_id=page_id or None,
                        error=error,
                    )
                )
                continue
            state.documents.append(document)
        return state

    def write_outputs(
        self,
        state: DataIngestionWorkflowState,
    ) -> DataIngestionWorkflowState:
        """Local file output 저장 node."""
        state.report = build_ingestion_report(
            job_id=state.job_id,
            spaces_total=len(state.spaces),
            page_refs_total=len(state.page_refs),
            pages_succeeded=len(state.page_details),
            documents_written=len(state.documents),
            failed_items=len(state.failed_items),
            skipped_items=0,
        )
        if _has_list_spaces_failure(state.failed_items):
            state.report.status = IngestionReportStatus.FAILED
        if state.spaces and state.failed_items and not state.documents:
            state.report.status = IngestionReportStatus.COMPLETED_WITH_ERRORS

        try:
            state.write_result = self.repository.write_outputs(
                documents=state.documents,
                failed_items=state.failed_items,
                report=state.report,
            )
        except Exception as error:
            state.failed_items.append(
                _failed_item_from_error(
                    job_id=state.job_id,
                    stage=FailedItemStage.WRITE_OUTPUT,
                    item_type=FailedItemType.DOCUMENT,
                    item_id=None,
                    error=error,
                )
            )
            state.report = build_ingestion_report(
                job_id=state.job_id,
                spaces_total=len(state.spaces),
                page_refs_total=len(state.page_refs),
                pages_succeeded=len(state.page_details),
                documents_written=len(state.documents),
                failed_items=len(state.failed_items),
                skipped_items=0,
            )
        return state

    def write_report(
        self,
        state: DataIngestionWorkflowState,
    ) -> DataIngestionWorkflowState:
        """Report 단계 node.

        feature4 local repository가 report 파일까지 저장하므로 이 node는 명세상 단계
        경계를 보존한다.
        """
        return state


def run_full_crawl_workflow(
    *,
    config: DataIngestionConfig,
    client: DataIngestionClient | None = None,
    repository: LocalFileRepository | None = None,
) -> DataIngestionWorkflowResult:
    """Data Ingestion full crawl workflow를 실행한다."""
    return DataIngestionWorkflowRunner(
        config=config,
        client=client,
        repository=repository,
    ).run()


def _space_info_from_payload(space_payload: dict[str, Any]) -> SpaceInfo:
    return SpaceInfo(
        space_id=str(space_payload.get("id") or ""),
        space_key=str(space_payload.get("key") or ""),
        space_name=str(space_payload.get("name") or ""),
    )


def _failed_item_from_error(
    *,
    job_id: str,
    stage: FailedItemStage,
    item_type: FailedItemType,
    item_id: str | None,
    error: Exception,
) -> FailedItem:
    if isinstance(error, ConfluenceApiError):
        return build_failed_item(
            job_id=job_id,
            stage=stage,
            item_type=item_type,
            item_id=item_id,
            error_type=error.error_type,
            error_message=_safe_message(str(error)),
            retryable=error.retryable,
            attempt_count=error.attempt_count,
        )

    return build_failed_item(
        job_id=job_id,
        stage=stage,
        item_type=item_type,
        item_id=item_id,
        error_type=type(error).__name__,
        error_message=_safe_message(str(error) or type(error).__name__),
        retryable=False,
        attempt_count=1,
    )


def _safe_message(message: str) -> str:
    return message.replace("Authorization", "<redacted>")


def _has_list_spaces_failure(failed_items: list[FailedItem]) -> bool:
    return any(
        failed_item.stage == FailedItemStage.LIST_SPACES
        for failed_item in failed_items
    )
