from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Query Routing Agent feature7 workflow orchestration과
          LangGraph optional fallback 구조 구현.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, sequential workflow/CLI 연동용 runner 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/json/pathlib 기반
--------------------------------------------------
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from query_routing_agent.config import QueryRoutingConfig
from query_routing_agent.llm import (
    IntentClassificationResult,
    OpenAIRoutingLLMProvider,
    RoutingLLMProvider,
    classify_intent,
)
from query_routing_agent.routing import (
    FilterAndWeightResult,
    NormalizedRoutingInputResult,
    QueryRewriteResult,
    RoutingOutputPaths,
    build_filter_and_pool_weights,
    build_routing_decision,
    build_routing_report,
    build_search_request_payload,
    load_history_manager_output,
    make_failed_item,
    normalize_routing_input,
    rewrite_queries,
    write_routing_outputs,
)
from query_routing_agent.schemas import (
    FailedItem,
    RoutingDecision,
    RoutingReport,
    RoutingReportStatus,
    SearchRequestPayload,
)

NODE_ORDER = [
    "load_config",
    "load_input",
    "normalize_routing_input",
    "classify_intent_and_rewrite",
    "build_metadata_filters",
    "build_pool_weights",
    "build_task_prompt_type",
    "build_routing_decision",
    "build_search_request",
    "write_output",
    "write_report",
]


@dataclass(slots=True)
class QueryRoutingWorkflowGraph:
    """Workflow builder가 반환하는 실행 방식 descriptor."""

    execution_mode: str


@dataclass(slots=True)
class QueryRoutingWorkflowState:
    """Query Routing workflow node 간 공유 state."""

    config: QueryRoutingConfig
    input_path: Path
    output_path: Path
    report_output_path: Path | None = None
    failed_output_path: Path | None = None
    raw_input: dict[str, Any] | None = None
    normalized_input: NormalizedRoutingInputResult | None = None
    classification: IntentClassificationResult | None = None
    rewrite_result: QueryRewriteResult | None = None
    filter_result: FilterAndWeightResult | None = None
    decision: RoutingDecision | None = None
    search_request: SearchRequestPayload | None = None
    report: RoutingReport | None = None
    failed_items: list[FailedItem] = field(default_factory=list)
    paths: RoutingOutputPaths | None = None
    node_order: list[str] = field(default_factory=list)


@dataclass(slots=True)
class QueryRoutingWorkflowResult:
    """Workflow 실행 결과."""

    status: str
    execution_mode: str
    node_order: list[str]
    paths: RoutingOutputPaths | None = None
    decision: RoutingDecision | None = None
    search_request: SearchRequestPayload | None = None
    report: RoutingReport | dict[str, Any] | None = None
    failed_items: list[FailedItem] = field(default_factory=list)

    def to_safe_dict(self) -> dict[str, Any]:
        """테스트와 report에 사용할 수 있는 primitive 결과를 반환한다."""
        return {
            "status": self.status,
            "execution_mode": self.execution_mode,
            "node_order": list(self.node_order),
            "paths": _paths_to_dict(self.paths),
            "decision": self.decision.to_dict() if self.decision else None,
            "search_request": (
                self.search_request.to_dict() if self.search_request else None
            ),
            "report": _report_to_dict(self.report),
            "failed_items": [item.to_dict() for item in self.failed_items],
        }


class QueryRoutingWorkflowRunner:
    """Query Routing workflow sequential fallback runner."""

    def __init__(
        self,
        config: QueryRoutingConfig,
        provider: RoutingLLMProvider | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.workflow = build_query_routing_workflow()

    def run(
        self,
        input_path: str | Path,
        output_path: str | Path,
        report_output_path: str | Path | None = None,
        failed_output_path: str | Path | None = None,
    ) -> QueryRoutingWorkflowResult:
        """Workflow를 실행하고 local JSON 산출물을 생성한다."""
        state = QueryRoutingWorkflowState(
            config=self.config,
            input_path=Path(input_path),
            output_path=Path(output_path),
            report_output_path=Path(report_output_path) if report_output_path else None,
            failed_output_path=Path(failed_output_path) if failed_output_path else None,
        )
        try:
            self._load_config(state)
            self._load_input(state)
            self._normalize_routing_input(state)
            self._classify_intent_and_rewrite(state)
            self._build_filter_weight_prompt(state)
            self._build_routing_decision(state)
            self._build_search_request(state)
            self._write_output(state)
            self._write_report(state)
        except Exception as exc:  # noqa: BLE001
            return self._handle_failure(state, exc)

        return QueryRoutingWorkflowResult(
            status=RoutingReportStatus.SUCCESS.value,
            execution_mode=self.workflow.execution_mode,
            node_order=state.node_order,
            paths=state.paths,
            decision=state.decision,
            search_request=state.search_request,
            report=state.report,
            failed_items=state.failed_items,
        )

    def _load_config(self, state: QueryRoutingWorkflowState) -> None:
        state.node_order.append("load_config")
        state.config.validate()

    def _load_input(self, state: QueryRoutingWorkflowState) -> None:
        state.node_order.append("load_input")
        state.raw_input = load_history_manager_output(state.input_path)

    def _normalize_routing_input(self, state: QueryRoutingWorkflowState) -> None:
        state.node_order.append("normalize_routing_input")
        if state.raw_input is None:
            raise ValueError("workflow input was not loaded")
        state.normalized_input = normalize_routing_input(state.raw_input)

    def _classify_intent_and_rewrite(self, state: QueryRoutingWorkflowState) -> None:
        state.node_order.append("classify_intent_and_rewrite")
        if state.normalized_input is None:
            raise ValueError("routing input was not normalized")
        provider = self.provider or OpenAIRoutingLLMProvider.from_config(state.config)
        state.classification = classify_intent(
            normalized_input=state.normalized_input,
            config=state.config,
            provider=provider,
        )
        state.rewrite_result = rewrite_queries(
            normalized_input=state.normalized_input,
            classification=state.classification,
            config=state.config,
        )

    def _build_filter_weight_prompt(self, state: QueryRoutingWorkflowState) -> None:
        state.node_order.append("build_metadata_filters")
        if state.normalized_input is None or state.classification is None:
            raise ValueError("classification result is required")
        state.filter_result = build_filter_and_pool_weights(
            normalized_input=state.normalized_input,
            intent=state.classification.intent,
            config=state.config,
        )
        state.node_order.append("build_pool_weights")
        state.node_order.append("build_task_prompt_type")

    def _build_routing_decision(self, state: QueryRoutingWorkflowState) -> None:
        state.node_order.append("build_routing_decision")
        if (
            state.normalized_input is None
            or state.classification is None
            or state.rewrite_result is None
            or state.filter_result is None
        ):
            raise ValueError("routing decision inputs are incomplete")
        state.decision = build_routing_decision(
            normalized_input=state.normalized_input,
            classification=state.classification,
            rewrite_result=state.rewrite_result,
            filter_result=state.filter_result,
            config=state.config,
        )

    def _build_search_request(self, state: QueryRoutingWorkflowState) -> None:
        state.node_order.append("build_search_request")
        if state.decision is None:
            raise ValueError("routing decision is required")
        state.search_request = build_search_request_payload(
            decision=state.decision,
            config=state.config,
        )
        state.report = build_routing_report(
            decision=state.decision,
            status=RoutingReportStatus.SUCCESS,
        )

    def _write_output(self, state: QueryRoutingWorkflowState) -> None:
        state.node_order.append("write_output")
        if state.decision is None or state.search_request is None or state.report is None:
            raise ValueError("workflow outputs are incomplete")
        state.paths = _write_success_outputs(state)

    def _write_report(self, state: QueryRoutingWorkflowState) -> None:
        state.node_order.append("write_report")

    def _handle_failure(
        self,
        state: QueryRoutingWorkflowState,
        exc: Exception,
    ) -> QueryRoutingWorkflowResult:
        failed_item = make_failed_item(
            item_id=state.input_path.name or "query-routing-workflow",
            reason=str(exc),
            retryable=bool(getattr(exc, "retryable", False)),
            error_type=str(getattr(exc, "code", exc.__class__.__name__)),
        )
        state.failed_items = [failed_item]
        paths = _failure_paths(state)
        _write_failure_outputs(paths, failed_item)
        return QueryRoutingWorkflowResult(
            status=RoutingReportStatus.FAILED.value,
            execution_mode=self.workflow.execution_mode,
            node_order=state.node_order,
            paths=paths,
            failed_items=state.failed_items,
            report=_failed_report_payload(failed_item),
        )


def is_langgraph_available() -> bool:
    """LangGraph 설치 여부를 확인한다."""
    try:
        import langgraph  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def build_query_routing_workflow() -> QueryRoutingWorkflowGraph:
    """LangGraph가 있으면 해당 mode를 표시하고, 없으면 sequential fallback을 사용한다."""
    return QueryRoutingWorkflowGraph(
        execution_mode="langgraph" if is_langgraph_available() else "sequential"
    )


def run_query_routing_workflow(
    input_path: str | Path,
    output_path: str | Path,
    config: QueryRoutingConfig,
    provider: RoutingLLMProvider | None = None,
    report_output_path: str | Path | None = None,
    failed_output_path: str | Path | None = None,
) -> QueryRoutingWorkflowResult:
    """Query Routing workflow 편의 실행 함수."""
    runner = QueryRoutingWorkflowRunner(config=config, provider=provider)
    return runner.run(
        input_path=input_path,
        output_path=output_path,
        report_output_path=report_output_path,
        failed_output_path=failed_output_path,
    )


def _write_success_outputs(state: QueryRoutingWorkflowState) -> RoutingOutputPaths:
    search_request_path = state.output_path.with_name("search_request.json")
    report_path = state.report_output_path or state.output_path.with_name(
        "routing_report.json"
    )
    failed_items_path = state.failed_output_path or state.output_path.with_name(
        "failed_items.json"
    )
    return write_routing_outputs(
        output_dir=state.output_path.parent,
        decision=state.decision,
        search_request=state.search_request,
        report=state.report,
        failed_items=state.failed_items,
        decision_path=state.output_path,
        search_request_path=search_request_path,
        report_path=report_path,
        failed_items_path=failed_items_path,
    )


def _failure_paths(state: QueryRoutingWorkflowState) -> RoutingOutputPaths:
    return RoutingOutputPaths(
        decision_path=state.output_path,
        search_request_path=state.output_path.with_name("search_request.json"),
        report_path=state.report_output_path
        or state.output_path.with_name("routing_report.json"),
        failed_items_path=state.failed_output_path
        or state.output_path.with_name("failed_items.json"),
    )


def _write_failure_outputs(paths: RoutingOutputPaths, failed_item: FailedItem) -> None:
    paths.report_path.parent.mkdir(parents=True, exist_ok=True)
    paths.failed_items_path.parent.mkdir(parents=True, exist_ok=True)
    paths.report_path.write_text(
        json.dumps(_failed_report_payload(failed_item), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    paths.failed_items_path.write_text(
        json.dumps([failed_item.to_dict()], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _failed_report_payload(failed_item: FailedItem) -> dict[str, Any]:
    return {
        "job_id": "job-query-routing-failed",
        "routing_id": "unavailable",
        "conversation_id": "unavailable",
        "status": RoutingReportStatus.FAILED.value,
        "intent": "unknown",
        "expanded_query_count": 0,
        "warnings_count": 0,
        "failed_count": 1,
        "error_type": failed_item.error_type,
        "created_at": _utc_now_iso(),
    }


def _paths_to_dict(paths: RoutingOutputPaths | None) -> dict[str, str] | None:
    if paths is None:
        return None
    return {
        "decision_path": str(paths.decision_path),
        "search_request_path": str(paths.search_request_path),
        "report_path": str(paths.report_path),
        "failed_items_path": str(paths.failed_items_path),
    }


def _report_to_dict(report: RoutingReport | dict[str, Any] | None) -> dict[str, Any] | None:
    if report is None:
        return None
    if isinstance(report, RoutingReport):
        return report.to_dict()
    return dict(report)


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
