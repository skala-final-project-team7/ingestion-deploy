from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Verification Agent rule-based verifier.
          feature4 범위에서는 parser 결과를 citation/context/token/numeric/source
          rule로 평가하고 sentence별 preliminary label과 score를 생성한다.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature4 rule-based verifier 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/re 기반
--------------------------------------------------
"""

import re
from dataclasses import dataclass, field
from typing import Any

from answer_verification_agent.schemas import SentenceLabel, WarningItem
from answer_verification_agent.schemas._serialization import to_primitive
from answer_verification_agent.verification.input_normalization import (
    NormalizedContext,
)
from answer_verification_agent.verification.sentence_parser import (
    ParsedSentence,
    SentenceCitationParseResult,
)

_SENSITIVE_PATTERNS = [
    re.compile(r"OPENAI_API_KEY\s*=\s*[^,\s]+", re.IGNORECASE),
    re.compile(r"Authorization:\s*Bearer\s+[^,\s]+", re.IGNORECASE),
    re.compile(r"Bearer\s+[^,\s]+", re.IGNORECASE),
]
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9가-힣_./%-]+")
_NUMERIC_PATTERN = re.compile(
    r"\bv?\d+(?:\.\d+){1,3}\b|\b\d{4}-\d{2}-\d{2}\b|\b\d+(?:\.\d+)?%\b|\b\d+(?:\.\d+)?\b",
    re.IGNORECASE,
)
_STOPWORDS = {
    "the",
    "and",
    "or",
    "a",
    "an",
    "to",
    "of",
    "in",
    "on",
    "for",
    "with",
    "is",
    "are",
    "was",
    "were",
    "be",
    "this",
    "that",
    "it",
}


@dataclass(slots=True)
class RuleVerifierConfig:
    """Rule threshold config."""

    min_token_overlap: float = 0.5
    source_coverage_threshold: float = 0.6
    supported_score_threshold: float = 0.75
    low_confidence_score_threshold: float = 0.4

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for field_name in (
            "min_token_overlap",
            "source_coverage_threshold",
            "supported_score_threshold",
            "low_confidence_score_threshold",
        ):
            value = getattr(self, field_name)
            if not 0 <= value <= 1:
                raise ValueError(f"{field_name} must be between 0 and 1")


@dataclass(slots=True)
class RuleCheckResult:
    """Single rule evaluation result."""

    rule_name: str
    passed: bool
    score_delta: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return _redact_payload(to_primitive(self))


@dataclass(slots=True)
class RuleVerifiedSentence:
    """Sentence-level preliminary rule verification result."""

    sentence_id: str
    text: str
    preliminary_label: SentenceLabel | str
    score: float
    citations: list[str]
    matched_context_ids: list[str]
    invalid_citations: list[str]
    passed_rules: list[str]
    failed_rules: list[str]
    rule_results: list[RuleCheckResult]
    token_overlap_ratio: float

    def to_dict(self) -> dict[str, Any]:
        return _redact_payload(to_primitive(self))


@dataclass(slots=True)
class RuleVerificationResult:
    """Rule-based verifier result."""

    sentence_results: list[RuleVerifiedSentence]
    failed_rules: list[str]
    warnings: list[WarningItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _redact_payload(
            {
                "sentence_results": [
                    sentence.to_dict() for sentence in self.sentence_results
                ],
                "failed_rules": list(self.failed_rules),
                "warnings": [warning.to_dict() for warning in self.warnings],
            }
        )


def run_rule_based_verification(
    parsed: SentenceCitationParseResult,
    contexts: list[NormalizedContext],
    *,
    config: RuleVerifierConfig | None = None,
) -> RuleVerificationResult:
    """Parser 결과를 rule set으로 평가한다."""
    rule_config = config or RuleVerifierConfig()
    rule_config.validate()
    context_by_id = {context.context_id: context for context in contexts}
    warnings = list(parsed.warnings)

    sentence_results = [
        _verify_sentence(sentence, context_by_id, rule_config)
        for sentence in parsed.sentences
    ]

    failed_rules: list[str] = []
    for sentence in sentence_results:
        for rule_name in sentence.failed_rules:
            if rule_name not in failed_rules:
                failed_rules.append(rule_name)

    if parsed.citation_coverage.coverage_ratio < rule_config.source_coverage_threshold:
        if "source_coverage" not in failed_rules:
            failed_rules.append("source_coverage")
        warnings.append(
            WarningItem(
                code="source_coverage_low",
                message="Citation source coverage is below configured threshold.",
            )
        )

    return RuleVerificationResult(
        sentence_results=sentence_results,
        failed_rules=failed_rules,
        warnings=warnings,
    )


def _verify_sentence(
    sentence: ParsedSentence,
    context_by_id: dict[str, NormalizedContext],
    config: RuleVerifierConfig,
) -> RuleVerifiedSentence:
    cited_text = " ".join(
        context_by_id[context_id].content
        for context_id in sentence.matched_context_ids
        if context_id in context_by_id
    )
    token_overlap_ratio = _token_overlap_ratio(sentence.text, cited_text)
    rule_results = [
        _citation_exists_rule(sentence),
        _valid_context_citation_rule(sentence),
        _token_overlap_rule(token_overlap_ratio, config),
        _number_date_version_presence_rule(sentence.text, cited_text),
    ]
    passed_rules = [result.rule_name for result in rule_results if result.passed]
    failed_rules = [result.rule_name for result in rule_results if not result.passed]
    score = _aggregate_score(rule_results)
    preliminary_label = _preliminary_label(
        score,
        failed_rules,
        config,
    )
    return RuleVerifiedSentence(
        sentence_id=sentence.sentence_id,
        text=sentence.text,
        preliminary_label=preliminary_label,
        score=score,
        citations=list(sentence.citations),
        matched_context_ids=list(sentence.matched_context_ids),
        invalid_citations=list(sentence.invalid_citations),
        passed_rules=passed_rules,
        failed_rules=failed_rules,
        rule_results=rule_results,
        token_overlap_ratio=token_overlap_ratio,
    )


def _citation_exists_rule(sentence: ParsedSentence) -> RuleCheckResult:
    passed = bool(sentence.citations)
    return RuleCheckResult(
        rule_name="citation_exists",
        passed=passed,
        score_delta=0.0 if passed else -0.35,
        reason="Sentence has at least one citation." if passed else "Sentence has no citation.",
    )


def _valid_context_citation_rule(sentence: ParsedSentence) -> RuleCheckResult:
    passed = bool(sentence.citations) and not sentence.invalid_citations and bool(
        sentence.matched_context_ids
    )
    return RuleCheckResult(
        rule_name="valid_context_citation",
        passed=passed,
        score_delta=0.0 if passed else -0.35,
        reason=(
            "All citations reference known contexts."
            if passed
            else "One or more citations do not reference known contexts."
        ),
    )


def _token_overlap_rule(
    token_overlap_ratio: float,
    config: RuleVerifierConfig,
) -> RuleCheckResult:
    passed = token_overlap_ratio >= config.min_token_overlap
    return RuleCheckResult(
        rule_name="token_overlap",
        passed=passed,
        score_delta=0.0 if passed else -0.3,
        reason=(
            "Sentence tokens overlap with cited context."
            if passed
            else "Sentence token overlap with cited context is below threshold."
        ),
    )


def _number_date_version_presence_rule(
    sentence_text: str,
    cited_text: str,
) -> RuleCheckResult:
    expressions = _numeric_expressions(sentence_text)
    if not expressions:
        return RuleCheckResult(
            rule_name="number_date_version_presence",
            passed=True,
            score_delta=0.0,
            reason="Sentence has no numeric/date/version expressions to verify.",
        )
    cited_text_lower = cited_text.lower()
    missing = [
        expression
        for expression in expressions
        if expression.lower() not in cited_text_lower
    ]
    passed = not missing
    return RuleCheckResult(
        rule_name="number_date_version_presence",
        passed=passed,
        score_delta=0.0 if passed else -0.3,
        reason=(
            "Numeric/date/version expressions are present in cited context."
            if passed
            else "Numeric/date/version expressions are missing from cited context."
        ),
    )


def _aggregate_score(rule_results: list[RuleCheckResult]) -> float:
    score = 1.0 + sum(result.score_delta for result in rule_results)
    return max(0.0, min(1.0, round(score, 4)))


def _preliminary_label(
    score: float,
    failed_rules: list[str],
    config: RuleVerifierConfig,
) -> SentenceLabel:
    if "valid_context_citation" in failed_rules and "citation_exists" not in failed_rules:
        return SentenceLabel.UNSUPPORTED
    if score >= config.supported_score_threshold and not failed_rules:
        return SentenceLabel.SUPPORTED
    if score >= config.low_confidence_score_threshold:
        return SentenceLabel.LOW_CONFIDENCE
    return SentenceLabel.UNSUPPORTED


def _token_overlap_ratio(sentence_text: str, cited_text: str) -> float:
    sentence_tokens = _tokens(sentence_text)
    if not sentence_tokens:
        return 0.0
    cited_tokens = set(_tokens(cited_text))
    if not cited_tokens:
        return 0.0
    overlap = [token for token in sentence_tokens if token in cited_tokens]
    return round(len(overlap) / len(sentence_tokens), 4)


def _tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_PATTERN.finditer(value.lower()):
        token = match.group(0).strip("._/%-")
        if len(token) < 2 or token in _STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def _numeric_expressions(value: str) -> list[str]:
    expressions: list[str] = []
    seen: set[str] = set()
    for match in _NUMERIC_PATTERN.finditer(value):
        expression = match.group(0)
        if expression not in seen:
            expressions.append(expression)
            seen.add(expression)
    return expressions


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
