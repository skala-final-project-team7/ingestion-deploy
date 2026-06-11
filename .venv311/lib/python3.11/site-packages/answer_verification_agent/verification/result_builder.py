from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Verification Agent verification result builder.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature7 result/report/QCA/regeneration 조립 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/hashlib/re 기반
--------------------------------------------------
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from answer_verification_agent.qca import build_qca_output
from answer_verification_agent.regeneration import build_regeneration_request
from answer_verification_agent.schemas import (
    FailedItem,
    QCAOutput,
    SentenceLabel,
    SentenceVerificationResult,
    UIWarning,
    VerificationOutput,
    VerificationOverallLabel,
    VerificationReport,
    VerificationReportStatus,
    WarningItem,
)
from answer_verification_agent.schemas._serialization import to_primitive
from answer_verification_agent.verification.input_normalization import (
    NormalizedVerificationInput,
)
from answer_verification_agent.verification.rule_based_verifier import (
    RuleVerificationResult,
    RuleVerifiedSentence,
)
from answer_verification_agent.verification.sentence_parser import (
    SentenceCitationParseResult,
)

_SENSITIVE_PATTERNS = [
    re.compile(r"OPENAI_API_KEY\s*=\s*[^,\s]+", re.IGNORECASE),
    re.compile(r"Authorization:\s*Bearer\s+[^,\s]+", re.IGNORECASE),
    re.compile(r"Bearer\s+[^,\s]+", re.IGNORECASE),
]
_HARD_UNSUPPORTED_RULES = {
    "valid_context_citation",
    "number_date_version_presence",
}


@dataclass(slots=True)
class VerificationBuildResult:
    """Feature7 build result aggregate."""

    output: VerificationOutput
    qca_output: QCAOutput
    report: VerificationReport
    failed_items: list[FailedItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _redact_payload(
            {
                "output": self.output.to_dict(),
                "qca_output": self.qca_output.to_dict(),
                "report": self.report.to_dict(),
                "failed_items": [item.to_dict() for item in self.failed_items],
            }
        )


def build_verification_result(
    *,
    normalized_input: NormalizedVerificationInput,
    parsed: SentenceCitationParseResult,
    rule_result: RuleVerificationResult,
    evaluator_results: dict[str, Any] | None = None,
    evaluator_failures: list[FailedItem] | None = None,
) -> VerificationBuildResult:
    """Rule/evaluator 결과를 canonical output/report/QCA/regeneration으로 조립한다."""
    evaluations = evaluator_results or {}
    failed_items = evaluator_failures or []
    sentence_results = [
        _merge_sentence_result(sentence, evaluations.get(sentence.sentence_id))
        for sentence in rule_result.sentence_results
    ]
    warnings = _merge_warnings(normalized_input, rule_result, failed_items)
    unsupported_claims = _unsupported_claims(sentence_results, evaluations)
    overall_label = _overall_label(
        normalized_input,
        sentence_results,
        warnings,
        failed_items,
    )
    overall_score = _overall_score(sentence_results)
    ui_warning = _ui_warning(overall_label, sentence_results, warnings)
    verification_id = _verification_id(
        normalized_input.answer_output.generation_id,
        normalized_input.verification_input.conversation_id,
    )
    regeneration_request = build_regeneration_request(
        normalized_input.answer_output.generation_id,
        [
            sentence.sentence_id
            for sentence in sentence_results
            if sentence.label == SentenceLabel.UNSUPPORTED
        ],
        unsupported_claims,
    )
    output = VerificationOutput(
        verification_id=verification_id,
        generation_id=normalized_input.answer_output.generation_id,
        conversation_id=normalized_input.verification_input.conversation_id,
        user_id=normalized_input.verification_input.user_id,
        overall_label=overall_label,
        overall_score=overall_score,
        sentence_results=sentence_results,
        unsupported_claims=unsupported_claims,
        citation_coverage=parsed.citation_coverage,
        llm_evaluation_used=any(
            sentence.llm_evaluation_used for sentence in sentence_results
        ),
        ui_warning_required=ui_warning.warning_level != "none",
        ui_warning=ui_warning,
        qca_candidate=True,
        qca_output_ref=f"qca-{verification_id}",
        regeneration_recommended=regeneration_request is not None,
        regeneration_request=regeneration_request,
        warnings=warnings,
    )
    qca_output = build_qca_output(output, normalized_input)
    report = _build_report(output, warnings)
    return VerificationBuildResult(
        output=output,
        qca_output=qca_output,
        report=report,
        failed_items=failed_items,
    )


def build_failed_item(
    *,
    item_id: str,
    reason: str,
    error_type: str,
    retryable: bool,
) -> FailedItem:
    """Safe failed item helper."""
    return FailedItem(
        item_id=item_id,
        reason=_redact_text(reason),
        retryable=retryable,
        error_type=error_type,
    )


def _merge_sentence_result(
    sentence: RuleVerifiedSentence,
    evaluation: Any | None,
) -> SentenceVerificationResult:
    hard_rule_failed = bool(_HARD_UNSUPPORTED_RULES.intersection(sentence.failed_rules))
    if hard_rule_failed:
        label = SentenceLabel.UNSUPPORTED
        score = min(sentence.score, evaluation.score if evaluation else sentence.score)
        reason = "Rule verification found invalid citation or numeric/date/version mismatch."
    elif evaluation is not None:
        label = evaluation.label
        score = evaluation.score
        reason = evaluation.reason
    else:
        label = sentence.preliminary_label
        score = sentence.score
        reason = (
            "Rule-based verification result was used without LLM evaluator result."
        )
    return SentenceVerificationResult(
        sentence_id=sentence.sentence_id,
        text=_redact_text(sentence.text),
        label=label,
        score=score,
        citations=list(sentence.citations),
        matched_context_ids=list(sentence.matched_context_ids),
        failed_rules=list(sentence.failed_rules),
        llm_evaluation_used=evaluation is not None,
        reason=_redact_text(reason),
    )


def _overall_label(
    normalized_input: NormalizedVerificationInput,
    sentence_results: list[SentenceVerificationResult],
    warnings: list[WarningItem],
    failed_items: list[FailedItem],
) -> VerificationOverallLabel:
    if normalized_input.answer_output.answer_status == "insufficient_context":
        return VerificationOverallLabel.LOW_CONFIDENCE
    if failed_items:
        return VerificationOverallLabel.LOW_CONFIDENCE
    labels = [sentence.label for sentence in sentence_results]
    if any(label == SentenceLabel.UNSUPPORTED for label in labels):
        return VerificationOverallLabel.UNSUPPORTED
    if any(label == SentenceLabel.LOW_CONFIDENCE for label in labels):
        return VerificationOverallLabel.LOW_CONFIDENCE
    if labels and all(label == SentenceLabel.SUPPORTED for label in labels):
        return (
            VerificationOverallLabel.SUPPORTED
            if warnings
            else VerificationOverallLabel.PASS
        )
    return VerificationOverallLabel.LOW_CONFIDENCE


def _overall_score(sentence_results: list[SentenceVerificationResult]) -> float:
    if not sentence_results:
        return 0.0
    return round(
        sum(sentence.score for sentence in sentence_results) / len(sentence_results),
        4,
    )


def _unsupported_claims(
    sentence_results: list[SentenceVerificationResult],
    evaluations: dict[str, Any],
) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for sentence in sentence_results:
        if sentence.label != SentenceLabel.UNSUPPORTED:
            continue
        evaluation = evaluations.get(sentence.sentence_id)
        eval_claims = evaluation.unsupported_claims if evaluation else []
        claims.append(
            {
                "sentence_id": sentence.sentence_id,
                "text": sentence.text,
                "reason": sentence.reason,
                "citations": list(sentence.citations),
                "unsupported_claims": [
                    _redact_text(claim) for claim in eval_claims
                ],
            }
        )
    return _redact_payload(claims)


def _ui_warning(
    overall_label: VerificationOverallLabel,
    sentence_results: list[SentenceVerificationResult],
    warnings: list[WarningItem],
) -> UIWarning:
    unsupported_count = sum(
        1 for sentence in sentence_results if sentence.label == SentenceLabel.UNSUPPORTED
    )
    low_confidence_count = sum(
        1
        for sentence in sentence_results
        if sentence.label == SentenceLabel.LOW_CONFIDENCE
    )
    reasons: list[str] = []
    if unsupported_count:
        reasons.append("unsupported_claims_detected")
    if low_confidence_count or overall_label == VerificationOverallLabel.LOW_CONFIDENCE:
        reasons.append("low_confidence_verification")
    if warnings:
        reasons.append("verification_warnings")
    if overall_label == VerificationOverallLabel.UNSUPPORTED:
        level = "high"
    elif reasons:
        level = "medium" if low_confidence_count else "low"
    else:
        level = "none"
    return UIWarning(warning_level=level, warning_reasons=reasons)


def _merge_warnings(
    normalized_input: NormalizedVerificationInput,
    rule_result: RuleVerificationResult,
    failed_items: list[FailedItem],
) -> list[WarningItem]:
    warnings: list[WarningItem] = []
    for warning in normalized_input.warnings + rule_result.warnings:
        if warning.code not in {item.code for item in warnings}:
            warnings.append(
                WarningItem(
                    code=warning.code,
                    message=_redact_text(warning.message),
                )
            )
    for warning in normalized_input.answer_output.warnings:
        code = str(warning.get("code") or "answer_generation_warning")
        message = str(warning.get("message") or "Answer generation warning.")
        if code not in {item.code for item in warnings}:
            warnings.append(WarningItem(code=code, message=_redact_text(message)))
    if failed_items and "evaluator_failure" not in {item.code for item in warnings}:
        warnings.append(
            WarningItem(
                code="evaluator_failure",
                message="Evaluator failure was recorded; rule result was preserved.",
            )
        )
    return warnings


def _build_report(
    output: VerificationOutput,
    warnings: list[WarningItem],
) -> VerificationReport:
    unsupported_count = sum(
        1
        for sentence in output.sentence_results
        if sentence.label == SentenceLabel.UNSUPPORTED
    )
    low_confidence_count = sum(
        1
        for sentence in output.sentence_results
        if sentence.label == SentenceLabel.LOW_CONFIDENCE
    )
    llm_evaluation_count = sum(
        1 for sentence in output.sentence_results if sentence.llm_evaluation_used
    )
    return VerificationReport(
        job_id=f"job-{output.verification_id}",
        verification_id=output.verification_id,
        generation_id=output.generation_id,
        conversation_id=output.conversation_id,
        status=VerificationReportStatus.SUCCESS,
        overall_label=output.overall_label,
        sentence_count=len(output.sentence_results),
        unsupported_count=unsupported_count,
        low_confidence_count=low_confidence_count,
        llm_evaluation_count=llm_evaluation_count,
        warnings_count=len(warnings),
        created_at=datetime.now(UTC).isoformat(),
    )


def _verification_id(generation_id: str, conversation_id: str) -> str:
    digest = hashlib.sha256(f"{generation_id}:{conversation_id}".encode()).hexdigest()
    return f"verification-{digest[:16]}"


def _redact_payload(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_payload(item) for key, item in value.items()}
    return value


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub("<redacted>", redacted)
    return redacted
