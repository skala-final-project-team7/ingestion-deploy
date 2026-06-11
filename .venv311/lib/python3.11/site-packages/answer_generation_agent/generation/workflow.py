from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Generation Agent feature7 workflow orchestration 구현.
          기존 service/helper를 연결하고 LangGraph optional fallback과 CLI 실행 결과를 제공한다.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, sequential workflow 및 optional LangGraph wrapper 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - LangGraph 미설치 환경에서는 sequential fallback 사용
--------------------------------------------------
"""

import json
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from answer_generation_agent.config import AnswerGenerationConfig
from answer_generation_agent.generation.answer_generation import (
    AnswerGenerationResult,
    AnswerGenerationService,
    AnswerLLMProvider,
    AnswerProviderError,
    FakeAnswerLLMProvider,
    OpenAIAnswerLLMProvider,
)
from answer_generation_agent.generation.answer_output_builder import (
    build_answer_output,
    build_failed_answer_output,
    build_failed_item,
    build_generation_report,
)
from answer_generation_agent.generation.citation_mapping import (
    CitationMappingResult,
    map_citations,
)
from answer_generation_agent.generation.input_normalization import (
    NormalizedGenerationInputResult,
    load_generation_input_json,
    normalize_generation_input,
)
from answer_generation_agent.generation.prompt_template import build_prompt_payload
from answer_generation_agent.schemas import (
    AnswerOutput,
    AnswerStatus,
    FailedItem,
    GenerationReport,
    GenerationReportStatus,
    StreamingOutput,
    WarningItem,
)

_EXCLUDED_CAPABILITIES = [
    "qdrant_search",
    "dense_sparse_embedding",
    "cross_encoder_reranking",
    "answer_verification_call",
    "sse_transport",
    "bff_api_call",
    "database_persistence",
    "qca_persistence",
    "feedback_persistence",
    "ui_response_formatting",
]
_REDACTION_MARKERS = (
    "OPENAI_API_KEY",
    "Authorization",
    "api key",
    "API key",
    "secret",
    "token",
    "synthetic-marker",
)


@dataclass(slots=True)
class AnswerGenerationWorkflow:
    """Workflow engine descriptor."""

    engine: str
    capabilities: list[str]


@dataclass(slots=True)
class AnswerGenerationWorkflowState:
    """Workflow node들이 공유하는 state."""

    input_path: Path
    output_path: Path
    report_output_path: Path
    failed_output_path: Path
    config: AnswerGenerationConfig
    provider: AnswerLLMProvider
    raw_input: dict[str, Any] | None = None
    normalized_input: NormalizedGenerationInputResult | None = None
    generation_result: AnswerGenerationResult | None = None
    citation_result: CitationMappingResult | None = None
    answer_output: AnswerOutput | None = None
    report: GenerationReport | None = None
    failed_item: FailedItem | None = None
    executed_nodes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AnswerGenerationWorkflowResult:
    """Workflow 실행 결과."""

    status: str
    answer_output: AnswerOutput
    report: GenerationReport
    output_path: Path
    report_path: Path
    failed_path: Path | None
    executed_nodes: list[str]
    engine: str
    excluded_capabilities: list[str]

    def to_safe_dict(self) -> dict[str, Any]:
        """CLI summary와 테스트에서 사용할 safe result를 반환한다."""
        return {
            "status": self.status,
            "answer_status": str(self.answer_output.answer_status),
            "generation_id": self.answer_output.generation_id,
            "output_path": str(self.output_path),
            "report_path": str(self.report_path),
            "failed_path": str(self.failed_path) if self.failed_path else None,
            "engine": self.engine,
            "executed_nodes": list(self.executed_nodes),
            "excluded_capabilities": list(self.excluded_capabilities),
        }


def build_workflow(*, prefer_langgraph: bool = True) -> AnswerGenerationWorkflow:
    """LangGraph 사용 가능 여부를 확인하되, MVP 기본은 sequential fallback으로 실행한다."""
    capabilities = ["langgraph_optional", "sequential_fallback"]
    if prefer_langgraph and _is_langgraph_available():
        capabilities.append("langgraph_available")
    return AnswerGenerationWorkflow(engine="sequential", capabilities=capabilities)


def create_provider(
    *,
    provider_name: str,
    config: AnswerGenerationConfig,
) -> AnswerLLMProvider:
    """Config 기반 provider를 생성한다. 기본 테스트는 fake provider를 사용한다."""
    if provider_name == "fake":
        return FakeAnswerLLMProvider(response=_default_fake_response())
    if provider_name == "openai":
        return OpenAIAnswerLLMProvider(api_key=config.openai_api_key)
    raise ValueError("provider must be one of: fake, openai")


def run_answer_generation_workflow(
    *,
    input_path: Path,
    output_path: Path,
    report_output_path: Path | None = None,
    failed_output_path: Path | None = None,
    config: AnswerGenerationConfig | None = None,
    provider: AnswerLLMProvider | None = None,
    provider_name: str = "fake",
) -> AnswerGenerationWorkflowResult:
    """Answer Generation feature7 sequential workflow를 실행한다."""
    runtime_config = config or AnswerGenerationConfig()
    runtime_config.validate()
    workflow = build_workflow()
    runtime_provider = provider or create_provider(
        provider_name=provider_name,
        config=runtime_config,
    )
    state = AnswerGenerationWorkflowState(
        input_path=Path(input_path),
        output_path=Path(output_path),
        report_output_path=Path(report_output_path or Path(output_path).with_name("generation_report.json")),
        failed_output_path=Path(failed_output_path or Path(output_path).with_name("failed_items.json")),
        config=runtime_config,
        provider=runtime_provider,
    )

    _load_config(state)
    try:
        _load_input(state)
        _normalize_generation_input_node(state)
        _validate_top_contexts(state)
        _assess_context_sufficiency(state)
        _build_task_prompt(state)
    except ValueError as exc:
        _build_input_failed_output_node(state, exc)
        _write_output(state)
        _write_report(state)
        _write_failed_item(state)
        assert state.answer_output is not None
        assert state.report is not None
        return AnswerGenerationWorkflowResult(
            status=state.report.status.value,
            answer_output=state.answer_output,
            report=state.report,
            output_path=state.output_path,
            report_path=state.report_output_path,
            failed_path=state.failed_output_path,
            executed_nodes=list(state.executed_nodes),
            engine=workflow.engine,
            excluded_capabilities=list(_EXCLUDED_CAPABILITIES),
        )
    try:
        _generate_answer(state)
        _map_sentence_citations(state)
        _build_answer_output_node(state)
    except AnswerProviderError as exc:
        _build_failed_output_node(state, exc)
    _write_output(state)
    _write_report(state)
    if state.failed_item is not None:
        _write_failed_item(state)

    assert state.answer_output is not None
    assert state.report is not None
    return AnswerGenerationWorkflowResult(
        status=state.report.status.value,
        answer_output=state.answer_output,
        report=state.report,
        output_path=state.output_path,
        report_path=state.report_output_path,
        failed_path=state.failed_output_path if state.failed_item is not None else None,
        executed_nodes=list(state.executed_nodes),
        engine=workflow.engine,
        excluded_capabilities=list(_EXCLUDED_CAPABILITIES),
    )


def _load_config(state: AnswerGenerationWorkflowState) -> None:
    state.config.validate()
    state.executed_nodes.append("load_config")


def _load_input(state: AnswerGenerationWorkflowState) -> None:
    state.raw_input = load_generation_input_json(state.input_path)
    state.executed_nodes.append("load_input")


def _normalize_generation_input_node(state: AnswerGenerationWorkflowState) -> None:
    assert state.raw_input is not None
    state.normalized_input = normalize_generation_input(
        state.raw_input,
        max_contexts=state.config.max_contexts,
    )
    state.executed_nodes.append("normalize_generation_input")


def _validate_top_contexts(state: AnswerGenerationWorkflowState) -> None:
    assert state.normalized_input is not None
    for context in state.normalized_input.normalized_contexts:
        context.validate()
    state.executed_nodes.append("validate_top_contexts")


def _assess_context_sufficiency(state: AnswerGenerationWorkflowState) -> None:
    assert state.normalized_input is not None
    state.normalized_input.insufficient_context_candidate = (
        len(state.normalized_input.normalized_contexts) == 0
    )
    state.executed_nodes.append("assess_context_sufficiency")


def _build_task_prompt(state: AnswerGenerationWorkflowState) -> None:
    assert state.normalized_input is not None
    build_prompt_payload(state.normalized_input)
    state.executed_nodes.append("build_task_prompt")


def _generate_answer(state: AnswerGenerationWorkflowState) -> None:
    assert state.normalized_input is not None
    service = AnswerGenerationService(provider=state.provider)
    state.generation_result = service.generate(
        normalized_input=state.normalized_input,
        config=state.config,
    )
    state.executed_nodes.append("generate_answer")


def _map_sentence_citations(state: AnswerGenerationWorkflowState) -> None:
    assert state.normalized_input is not None
    state.citation_result = map_citations(
        generation_result=state.generation_result,
        normalized_input=state.normalized_input,
    )
    state.executed_nodes.append("map_sentence_citations")


def _build_answer_output_node(state: AnswerGenerationWorkflowState) -> None:
    assert state.normalized_input is not None
    state.answer_output = build_answer_output(
        normalized_input=state.normalized_input,
        generation_result=state.generation_result,
        citation_result=state.citation_result,
    )
    state.report = build_generation_report(
        answer_output=state.answer_output,
        normalized_input=state.normalized_input,
    )
    state.executed_nodes.append("build_answer_output")


def _build_failed_output_node(
    state: AnswerGenerationWorkflowState,
    error: AnswerProviderError,
) -> None:
    assert state.normalized_input is not None
    state.answer_output = build_failed_answer_output(
        normalized_input=state.normalized_input,
        error=error,
        model=state.config.model,
    )
    state.failed_item = build_failed_item(
        item_id=state.answer_output.generation_id,
        error=error,
    )
    state.report = build_generation_report(
        answer_output=state.answer_output,
        normalized_input=state.normalized_input,
    )
    state.report.status = GenerationReportStatus.FAILED
    state.executed_nodes.append("build_answer_output")


def _build_input_failed_output_node(
    state: AnswerGenerationWorkflowState,
    error: ValueError,
) -> None:
    conversation_id = _safe_payload_value(state.raw_input, "conversation_id", "unknown-conversation")
    user_id = _safe_payload_value(state.raw_input, "user_id", "unknown-user")
    routing_payload = state.raw_input.get("routing_decision", {}) if state.raw_input else {}
    routing_id = _redact_text(str(routing_payload.get("routing_id") or "unknown-routing"))
    generation_id = _input_error_generation_id(state.input_path)
    safe_error = _redact_text(str(error) or "Generation input error.")
    state.answer_output = AnswerOutput(
        generation_id=generation_id,
        conversation_id=conversation_id,
        user_id=user_id,
        answer_status=AnswerStatus.FAILED,
        answer="Answer generation failed.",
        sentences=[],
        sources=[],
        used_context_ids=[],
        routing={
            "routing_id": routing_id,
            "intent": _redact_text(str(routing_payload.get("intent") or "unknown")),
            "task_prompt_type": _redact_text(
                str(routing_payload.get("task_prompt_type") or "general")
            ),
        },
        model=_redact_text(state.config.model),
        confidence=0.0,
        insufficient_context=False,
        unsupported_gaps=[],
        streaming=StreamingOutput(streaming_supported=False, stream_chunks=[]),
        warnings=[
            WarningItem(
                code="input_error",
                message=safe_error,
            )
        ],
    )
    state.failed_item = FailedItem(
        item_id=generation_id,
        reason=safe_error,
        retryable=False,
        error_type="input_error",
    )
    state.report = GenerationReport(
        job_id=f"job-{generation_id.removeprefix('generation-')}",
        generation_id=generation_id,
        conversation_id=conversation_id,
        status=GenerationReportStatus.FAILED,
        answer_status=AnswerStatus.FAILED,
        context_count=0,
        used_context_count=0,
        sentence_count=0,
        citation_count=0,
        warnings_count=len(state.answer_output.warnings),
        created_at="1970-01-01T00:00:00Z",
    )
    state.executed_nodes.append("build_answer_output")


def _write_output(state: AnswerGenerationWorkflowState) -> None:
    assert state.answer_output is not None
    _write_json(state.output_path, state.answer_output.to_dict())
    state.executed_nodes.append("write_output")


def _write_report(state: AnswerGenerationWorkflowState) -> None:
    assert state.report is not None
    _write_json(state.report_output_path, state.report.to_dict())
    state.executed_nodes.append("write_report")


def _write_failed_item(state: AnswerGenerationWorkflowState) -> None:
    assert state.failed_item is not None
    _write_json(state.failed_output_path, {"failed_items": [state.failed_item.to_dict()]})


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_sanitize_value(payload), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _default_fake_response() -> dict[str, Any]:
    answer = "Synthetic answer generated from provided context."
    return {
        "answer": answer,
        "sentences": [{"text": answer, "citations": ["ctx-001"]}],
        "unsupported_gaps": [],
    }


def _is_langgraph_available() -> bool:
    try:
        __import__("langgraph")
    except ImportError:
        return False
    return True


def _input_error_generation_id(input_path: Path) -> str:
    digest = hashlib.sha256(str(input_path).encode("utf-8")).hexdigest()[:16]
    return f"generation-input-error-{digest}"


def _safe_payload_value(
    payload: dict[str, Any] | None,
    key: str,
    default: str,
) -> str:
    if not isinstance(payload, dict):
        return default
    value = str(payload.get(key) or default)
    return _redact_text(value)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_value(item)
            for key, item in value.items()
            if not _is_sensitive_key(str(key))
        }
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(marker in normalized for marker in ("api_key", "authorization", "token", "secret"))


def _redact_text(text: str) -> str:
    redacted = text
    for marker in _REDACTION_MARKERS:
        redacted = redacted.replace(marker, "<redacted>")
    return redacted
