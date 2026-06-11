from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : History Manager Agent workflow orchestration 및 local JSON output 구현.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature6 workflow 및 sequential fallback 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - LangGraph optional, 표준 라이브러리 dataclasses/json/pathlib 기반 fallback 제공
--------------------------------------------------
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from history_manager_agent.config import HistoryManagerConfig
from history_manager_agent.context import ContextPolicyResult, apply_context_policy
from history_manager_agent.history import (
    NormalizedHistoryResult,
    load_and_normalize_history_input,
)
from history_manager_agent.llm import (
    FakeHistoryLLMProvider,
    HistoryClassification,
    HistoryLLMProvider,
    LLMProviderError,
    classify_history,
)
from history_manager_agent.question import (
    ContextualizedQuestionResult,
    build_history_decision,
    build_query_routing_input,
    build_question_result,
)
from history_manager_agent.schemas import (
    HistoryDecision,
    HistoryDecisionLabel,
    HistoryFailedItem,
    HistoryReport,
    HistoryReportStatus,
    QueryRoutingInput,
)

NODE_SEQUENCE = [
    "load_config",
    "load_input",
    "normalize_history",
    "trim_history",
    "classify_history",
    "apply_context_policy",
    "build_contextualized_question",
    "build_routing_input",
    "write_output",
    "write_report",
]

MVP_EXCLUDED_CAPABILITIES = {
    "bff_api_adapter": "not_supported_in_mvp",
    "conversation_db_repository": "not_supported_in_mvp",
    "rag_search": "not_supported_in_mvp",
    "query_routing_agent": "not_supported_in_mvp",
    "answer_generation_agent": "not_supported_in_mvp",
    "answer_verification_agent": "not_supported_in_mvp",
    "sse_streaming": "not_supported_in_mvp",
}


@dataclass(slots=True)
class HistoryManagerWorkflowState:
    """Workflow node 간 공유 상태."""

    input_path: Path
    output_path: Path
    report_output_path: Path | None
    config: HistoryManagerConfig
    provider: HistoryLLMProvider
    node_trace: list[str] = field(default_factory=list)
    normalized_history: NormalizedHistoryResult | None = None
    classification: HistoryClassification | None = None
    context_policy: ContextPolicyResult | None = None
    question_result: ContextualizedQuestionResult | None = None
    decision: HistoryDecision | None = None
    routing_input: QueryRoutingInput | None = None
    failed_items: list[HistoryFailedItem] = field(default_factory=list)


@dataclass(slots=True)
class HistoryManagerWorkflowResult:
    """Workflow 실행 결과."""

    status: str
    output_path: Path
    report_output_path: Path | None
    failed_items: list[HistoryFailedItem] = field(default_factory=list)
    node_trace: list[str] = field(default_factory=list)
    execution_mode: str = "sequential_fallback"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "output_path": str(self.output_path),
            "report_output_path": str(self.report_output_path)
            if self.report_output_path
            else None,
            "failed_items": [item.to_dict() for item in self.failed_items],
            "node_trace": list(self.node_trace),
            "execution_mode": self.execution_mode,
        }


class HistoryManagerWorkflow:
    """LangGraph optional wrapper와 sequential fallback을 제공하는 workflow."""

    def __init__(self, execution_mode: str = "sequential_fallback") -> None:
        self.execution_mode = execution_mode

    def run(self, state: HistoryManagerWorkflowState) -> HistoryManagerWorkflowResult:
        try:
            _node_load_config(state)
            _node_load_input_normalize_trim(state)
            _node_classify_history(state)
            _node_apply_context_policy(state)
            _node_build_contextualized_question(state)
            _node_build_routing_input(state)
            _node_write_output(state)
            _node_write_report(state, status=HistoryReportStatus.SUCCESS)
            return HistoryManagerWorkflowResult(
                status=HistoryReportStatus.SUCCESS.value,
                output_path=state.output_path,
                report_output_path=state.report_output_path,
                failed_items=state.failed_items,
                node_trace=state.node_trace,
                execution_mode=self.execution_mode,
            )
        except Exception as exc:
            failed_item = _failed_item_from_exception(exc)
            state.failed_items.append(failed_item)
            _write_failed_output(state, failed_item)
            return HistoryManagerWorkflowResult(
                status=HistoryReportStatus.FAILED.value,
                output_path=state.output_path,
                report_output_path=state.report_output_path,
                failed_items=state.failed_items,
                node_trace=state.node_trace,
                execution_mode=self.execution_mode,
            )


def build_history_manager_workflow(
    use_langgraph: bool = True,
) -> HistoryManagerWorkflow:
    """LangGraph 사용 가능 여부를 확인하고 fallback-capable workflow를 반환한다."""
    if use_langgraph:
        try:
            import langgraph  # noqa: F401

            return HistoryManagerWorkflow(execution_mode="langgraph")
        except ImportError:
            return HistoryManagerWorkflow(execution_mode="sequential_fallback")
    return HistoryManagerWorkflow(execution_mode="sequential_fallback")


def run_history_manager_workflow(
    input_path: str | Path,
    output_path: str | Path,
    config: HistoryManagerConfig,
    provider: HistoryLLMProvider | None = None,
    report_output_path: str | Path | None = None,
    use_langgraph: bool = True,
) -> HistoryManagerWorkflowResult:
    """History Manager workflow를 실행한다."""
    selected_provider = provider or FakeHistoryLLMProvider(
        {
            "history_decision": "new_topic",
            "confidence": 1.0,
            "reason": "Empty or default fake provider classification.",
        }
    )
    state = HistoryManagerWorkflowState(
        input_path=Path(input_path),
        output_path=Path(output_path),
        report_output_path=Path(report_output_path) if report_output_path else None,
        config=config,
        provider=selected_provider,
    )
    workflow = build_history_manager_workflow(use_langgraph=use_langgraph)
    return workflow.run(state)


def _node_load_config(state: HistoryManagerWorkflowState) -> None:
    state.node_trace.append("load_config")
    state.config.validate()


def _node_load_input_normalize_trim(state: HistoryManagerWorkflowState) -> None:
    state.node_trace.extend(["load_input", "normalize_history", "trim_history"])
    state.normalized_history = load_and_normalize_history_input(
        state.input_path,
        state.config,
    )


def _node_classify_history(state: HistoryManagerWorkflowState) -> None:
    state.node_trace.append("classify_history")
    normalized_history = _require(state.normalized_history)
    if normalized_history.used_turn_count == 0:
        state.classification = HistoryClassification(
            history_decision=HistoryDecisionLabel.NEW_TOPIC,
            confidence=1.0,
            reason="Empty history is treated as a new topic.",
        )
        return
    state.classification = classify_history(
        normalized_history=normalized_history,
        config=state.config,
        provider=state.provider,
    )


def _node_apply_context_policy(state: HistoryManagerWorkflowState) -> None:
    state.node_trace.append("apply_context_policy")
    state.context_policy = apply_context_policy(
        normalized_history=_require(state.normalized_history),
        classification=_require(state.classification),
    )


def _node_build_contextualized_question(
    state: HistoryManagerWorkflowState,
) -> None:
    state.node_trace.append("build_contextualized_question")
    normalized_history = _require(state.normalized_history)
    state.question_result = build_question_result(
        conversation_id=normalized_history.history_input.conversation_id,
        user_id=normalized_history.history_input.user_id,
        current_question=normalized_history.history_input.current_question,
        policy_result=_require(state.context_policy),
        metadata=normalized_history.history_input.metadata,
    )
    state.decision = build_history_decision(state.question_result)


def _node_build_routing_input(state: HistoryManagerWorkflowState) -> None:
    state.node_trace.append("build_routing_input")
    state.routing_input = build_query_routing_input(_require(state.question_result))


def _node_write_output(state: HistoryManagerWorkflowState) -> None:
    state.node_trace.append("write_output")
    payload = {
        "status": HistoryReportStatus.SUCCESS.value,
        "decision": _require(state.decision).to_dict(),
        "routing_input": _require(state.routing_input).to_dict(),
        "execution": {"node_trace": list(state.node_trace)},
        "mvp_scope": _mvp_scope(),
    }
    _write_json(state.output_path, payload)


def _node_write_report(
    state: HistoryManagerWorkflowState,
    status: HistoryReportStatus,
) -> None:
    state.node_trace.append("write_report")
    report_path = state.report_output_path
    if report_path is None:
        return
    normalized_history = state.normalized_history
    decision = state.decision.history_decision if state.decision else "ambiguous"
    warnings_count = (
        len(state.question_result.warnings) if state.question_result else 0
    )
    report = HistoryReport(
        job_id=f"history-job-{uuid4().hex}",
        conversation_id=_conversation_id(state),
        status=status,
        decision=decision,
        input_turn_count=normalized_history.input_turn_count
        if normalized_history
        else 0,
        used_turn_count=normalized_history.used_turn_count
        if normalized_history
        else 0,
        warnings_count=warnings_count,
        created_at=_now_iso(),
    ).to_dict()
    if state.failed_items:
        report["failed_items"] = [item.to_dict() for item in state.failed_items]
    report["execution"] = {
        "node_trace": list(state.node_trace),
    }
    report["mvp_scope"] = _mvp_scope()
    _write_json(report_path, report)


def _write_failed_output(
    state: HistoryManagerWorkflowState,
    failed_item: HistoryFailedItem,
) -> None:
    payload = {
        "status": HistoryReportStatus.FAILED.value,
        "failed_items": [failed_item.to_dict()],
        "execution": {"node_trace": list(state.node_trace)},
        "mvp_scope": _mvp_scope(),
    }
    _write_json(state.output_path, payload)
    _node_write_report(state, status=HistoryReportStatus.FAILED)


def _failed_item_from_exception(exc: Exception) -> HistoryFailedItem:
    retryable = bool(getattr(exc, "retryable", False))
    error_type = getattr(exc, "code", exc.__class__.__name__)
    return HistoryFailedItem(
        stage="workflow",
        error_type=_safe_text(str(error_type)),
        error_message=_safe_text(str(exc) or exc.__class__.__name__),
        retryable=retryable,
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _conversation_id(state: HistoryManagerWorkflowState) -> str:
    if state.normalized_history:
        return state.normalized_history.history_input.conversation_id
    return "unknown"


def _safe_text(value: str) -> str:
    redacted = value
    replacements = {
        "OPENAI_API_KEY": "<redacted>",
        "Authorization": "<redacted>",
        "Bearer": "<redacted>",
        "API key": "credential",
        "api key": "credential",
        "secret-like": "<redacted>",
    }
    for source, target in replacements.items():
        redacted = redacted.replace(source, target)
    return redacted


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _mvp_scope() -> dict[str, Any]:
    return {
        "excluded_capabilities": dict(MVP_EXCLUDED_CAPABILITIES),
    }


def _require(value: Any) -> Any:
    if value is None:
        raise ValueError("workflow state is incomplete")
    return value
