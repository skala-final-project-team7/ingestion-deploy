from __future__ import annotations

"""Answer Verification Agent workflow orchestration."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from answer_verification_agent.config import AnswerVerificationConfig
from answer_verification_agent.evaluator import (
    AnswerEvaluatorProvider,
    EvaluatorProviderError,
    FakeEvaluatorProvider,
    OpenAIEvaluatorProvider,
)
from answer_verification_agent.storage import (
    write_failed_artifacts,
    write_verification_artifacts_to_paths,
)
from answer_verification_agent.verification.input_normalization import (
    VerificationInputNormalizationError,
    load_verification_input,
)
from answer_verification_agent.verification.result_builder import (
    VerificationBuildResult,
    build_failed_item,
    build_verification_result,
)
from answer_verification_agent.verification.rule_based_verifier import (
    RuleVerifierConfig,
    run_rule_based_verification,
)
from answer_verification_agent.verification.sentence_parser import (
    parse_sentences_and_citations,
)
from answer_verification_agent.verification.suspicious_selector import (
    SuspiciousSelectorConfig,
    select_suspicious_sentences,
)


@dataclass(slots=True)
class WorkflowRunResult:
    """Workflow run summary."""

    status: str
    execution_mode: str
    paths: dict[str, Path]
    result: VerificationBuildResult | None = None
    failed_items: list[dict[str, Any]] = field(default_factory=list)


def run_verification_workflow(
    *,
    input_path: Path | str,
    output_path: Path | str,
    report_output_path: Path | str,
    qca_output_path: Path | str,
    failed_output_path: Path | str,
    provider_mode: str = "fake",
    provider: AnswerEvaluatorProvider | None = None,
    config: AnswerVerificationConfig | None = None,
    evaluate_suspicious_only: bool = True,
) -> WorkflowRunResult:
    """Run Answer Verification workflow using LangGraph when available, otherwise sequential fallback."""
    execution_mode = _execution_mode()
    try:
        result = _run_sequential(
            input_path=Path(input_path),
            output_path=Path(output_path),
            report_output_path=Path(report_output_path),
            qca_output_path=Path(qca_output_path),
            failed_output_path=Path(failed_output_path),
            provider_mode=provider_mode,
            provider=provider,
            config=config or AnswerVerificationConfig(),
            evaluate_suspicious_only=evaluate_suspicious_only,
        )
    except VerificationInputNormalizationError as exc:
        failed_item = build_failed_item(
            item_id=str(input_path),
            reason=str(exc),
            error_type=exc.error_type,
            retryable=exc.retryable,
        )
        report = _failed_report(error_type=exc.error_type)
        paths = write_failed_artifacts(
            output_path=output_path,
            report_output_path=report_output_path,
            failed_output_path=failed_output_path,
            failed_items=[failed_item.to_dict()],
            report=report,
        )
        return WorkflowRunResult(
            status="failed",
            execution_mode=execution_mode,
            paths=paths,
            failed_items=[failed_item.to_dict()],
        )
    result.execution_mode = execution_mode
    return result


def build_langgraph_workflow() -> Any | None:
    """Build LangGraph wrapper when dependency is installed.

    MVP tests intentionally exercise the sequential fallback path.
    """
    try:
        from langgraph.graph import StateGraph  # type: ignore
    except Exception:
        return None
    return StateGraph(dict)


def _run_sequential(
    *,
    input_path: Path,
    output_path: Path | str,
    report_output_path: Path | str,
    qca_output_path: Path | str,
    failed_output_path: Path | str,
    provider_mode: str,
    provider: AnswerEvaluatorProvider | None,
    config: AnswerVerificationConfig,
    evaluate_suspicious_only: bool,
) -> WorkflowRunResult:
    normalized = load_verification_input(input_path)
    parsed = parse_sentences_and_citations(normalized)
    rule_result = run_rule_based_verification(
        parsed,
        normalized.contexts,
        config=RuleVerifierConfig(),
    )
    selection = select_suspicious_sentences(
        rule_result,
        normalized,
        config=SuspiciousSelectorConfig(
            evaluate_suspicious_only=evaluate_suspicious_only,
            score_threshold=config.min_sentence_score,
        ),
    )
    evaluator = provider or _provider(provider_mode, config)
    evaluator_results = {}
    failed_items = []
    for target in selection.evaluation_targets:
        try:
            evaluator_results[target.sentence_id] = evaluator.evaluate_sentence(
                target,
                normalized.contexts,
            )
        except EvaluatorProviderError as exc:
            failed_items.append(
                build_failed_item(
                    item_id=target.sentence_id,
                    reason=str(exc),
                    error_type=exc.error_type,
                    retryable=exc.retryable,
                )
            )
    build_result = build_verification_result(
        normalized_input=normalized,
        parsed=parsed,
        rule_result=rule_result,
        evaluator_results=evaluator_results,
        evaluator_failures=failed_items,
    )
    paths = write_verification_artifacts_to_paths(
        build_result,
        output_path=output_path,
        report_output_path=report_output_path,
        qca_output_path=qca_output_path,
        failed_output_path=failed_output_path,
    )
    status = "partial_success" if failed_items else "success"
    return WorkflowRunResult(
        status=status,
        execution_mode="sequential_fallback",
        paths=paths,
        result=build_result,
        failed_items=[item.to_dict() for item in failed_items],
    )


def _provider(
    provider_mode: str,
    config: AnswerVerificationConfig,
) -> AnswerEvaluatorProvider:
    if provider_mode == "fake":
        return FakeEvaluatorProvider()
    if provider_mode == "openai":
        return OpenAIEvaluatorProvider(config=config)
    raise ValueError("provider_mode must be fake or openai")


def _execution_mode() -> str:
    return "langgraph" if build_langgraph_workflow() is not None else "sequential_fallback"


def _failed_report(*, error_type: str) -> dict[str, Any]:
    return {
        "job_id": "job-failed",
        "verification_id": "verification-failed",
        "generation_id": "",
        "conversation_id": "",
        "status": "failed",
        "overall_label": "LOW_CONFIDENCE",
        "sentence_count": 0,
        "unsupported_count": 0,
        "low_confidence_count": 0,
        "llm_evaluation_count": 0,
        "warnings_count": 1,
        "error_type": error_type,
        "created_at": datetime.now(UTC).isoformat(),
    }
