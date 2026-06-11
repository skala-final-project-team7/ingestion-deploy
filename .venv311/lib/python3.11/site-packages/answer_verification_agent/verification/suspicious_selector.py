from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Verification Agent suspicious sentence selector.
          feature5 범위에서는 rule-based verifier 결과를 기반으로 LLM evaluator
          대상 문장을 선정한다.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature5 suspicious sentence selector 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/re 기반
--------------------------------------------------
"""

import re
from dataclasses import dataclass
from typing import Any

from answer_verification_agent.schemas._serialization import to_primitive
from answer_verification_agent.verification.input_normalization import (
    NormalizedVerificationInput,
)
from answer_verification_agent.verification.rule_based_verifier import (
    RuleVerificationResult,
    RuleVerifiedSentence,
)

_SENSITIVE_PATTERNS = [
    re.compile(r"OPENAI_API_KEY\s*=\s*[^,\s]+", re.IGNORECASE),
    re.compile(r"Authorization:\s*Bearer\s+[^,\s]+", re.IGNORECASE),
    re.compile(r"Bearer\s+[^,\s]+", re.IGNORECASE),
]
_FAILED_RULE_REASON_MAP = {
    "citation_exists": "citation_missing",
    "valid_context_citation": "invalid_citation",
    "token_overlap": "low_token_overlap",
    "number_date_version_presence": "number_date_version_mismatch",
}
_REASON_ORDER = [
    "citation_missing",
    "invalid_citation",
    "low_token_overlap",
    "number_date_version_mismatch",
    "insufficient_context",
    "score_below_threshold",
    "answer_generation_warning",
    "all_sentence_evaluation",
]


@dataclass(slots=True)
class SuspiciousSelectorConfig:
    """Selector policy config."""

    evaluate_suspicious_only: bool = True
    score_threshold: float = 0.6

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.evaluate_suspicious_only, bool):
            raise ValueError("evaluate_suspicious_only must be a boolean")
        if not 0 <= self.score_threshold <= 1:
            raise ValueError("score_threshold must be between 0 and 1")


@dataclass(slots=True)
class SuspiciousSentenceTarget:
    """LLM evaluator target sentence."""

    sentence_id: str
    text: str
    score: float
    preliminary_label: str
    reasons: list[str]
    citations: list[str]
    matched_context_ids: list[str]
    invalid_citations: list[str]
    failed_rules: list[str]

    def to_dict(self) -> dict[str, Any]:
        return _redact_payload(to_primitive(self))


@dataclass(slots=True)
class SuspiciousSelectionResult:
    """Suspicious sentence selection result."""

    evaluation_targets: list[SuspiciousSentenceTarget]
    suspicious_sentence_ids: list[str]
    evaluate_suspicious_only: bool

    def to_dict(self) -> dict[str, Any]:
        return _redact_payload(
            {
                "evaluation_targets": [
                    target.to_dict() for target in self.evaluation_targets
                ],
                "suspicious_sentence_ids": list(self.suspicious_sentence_ids),
                "evaluate_suspicious_only": self.evaluate_suspicious_only,
            }
        )


def select_suspicious_sentences(
    rule_result: RuleVerificationResult,
    normalized_input: NormalizedVerificationInput,
    *,
    config: SuspiciousSelectorConfig | None = None,
) -> SuspiciousSelectionResult:
    """Rule 결과에서 LLM evaluator 대상 문장을 선정한다."""
    selector_config = config or SuspiciousSelectorConfig()
    selector_config.validate()
    answer_status = normalized_input.answer_output.answer_status
    has_answer_generation_warning = bool(normalized_input.answer_output.warnings)
    insufficient_context = (
        answer_status == "insufficient_context"
        or normalized_input.low_confidence_ready
        or not normalized_input.has_contexts
    )

    targets: list[SuspiciousSentenceTarget] = []
    suspicious_sentence_ids: list[str] = []
    for sentence in rule_result.sentence_results:
        reasons = _suspicious_reasons(
            sentence,
            insufficient_context=insufficient_context,
            has_answer_generation_warning=has_answer_generation_warning,
            score_threshold=selector_config.score_threshold,
        )
        if reasons:
            suspicious_sentence_ids.append(sentence.sentence_id)
        if reasons or not selector_config.evaluate_suspicious_only:
            target_reasons = reasons or ["all_sentence_evaluation"]
            targets.append(_target_from_sentence(sentence, target_reasons))

    return SuspiciousSelectionResult(
        evaluation_targets=targets,
        suspicious_sentence_ids=suspicious_sentence_ids,
        evaluate_suspicious_only=selector_config.evaluate_suspicious_only,
    )


def _suspicious_reasons(
    sentence: RuleVerifiedSentence,
    *,
    insufficient_context: bool,
    has_answer_generation_warning: bool,
    score_threshold: float,
) -> list[str]:
    reasons: list[str] = []
    for failed_rule in sentence.failed_rules:
        reason = _FAILED_RULE_REASON_MAP.get(failed_rule)
        if reason:
            reasons.append(reason)
    if sentence.invalid_citations:
        reasons.append("invalid_citation")
    if insufficient_context:
        reasons.append("insufficient_context")
    if sentence.score < score_threshold:
        reasons.append("score_below_threshold")
    if has_answer_generation_warning:
        reasons.append("answer_generation_warning")
    return _unique_stable_reasons(reasons)


def _target_from_sentence(
    sentence: RuleVerifiedSentence,
    reasons: list[str],
) -> SuspiciousSentenceTarget:
    return SuspiciousSentenceTarget(
        sentence_id=sentence.sentence_id,
        text=sentence.text,
        score=sentence.score,
        preliminary_label=str(sentence.preliminary_label),
        reasons=reasons,
        citations=list(sentence.citations),
        matched_context_ids=list(sentence.matched_context_ids),
        invalid_citations=list(sentence.invalid_citations),
        failed_rules=list(sentence.failed_rules),
    )


def _unique_stable_reasons(reasons: list[str]) -> list[str]:
    unique = set(reasons)
    return [reason for reason in _REASON_ORDER if reason in unique]


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
