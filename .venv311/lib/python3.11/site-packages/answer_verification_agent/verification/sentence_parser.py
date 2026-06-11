from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Verification Agent sentence/citation parser.
          feature3 범위에서는 rule 판정 없이 문장, citation, context id 유효성,
          citation coverage만 계산해 후속 rule verifier 입력을 준비한다.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature3 sentence/citation parser 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/re 기반
--------------------------------------------------
"""

import re
from dataclasses import dataclass, field
from typing import Any

from answer_verification_agent.schemas import CitationCoverage, WarningItem
from answer_verification_agent.schemas._serialization import to_primitive
from answer_verification_agent.verification.input_normalization import (
    NormalizedVerificationInput,
)

_SENSITIVE_PATTERNS = [
    re.compile(r"OPENAI_API_KEY\s*=\s*[^,\s]+", re.IGNORECASE),
    re.compile(r"Authorization:\s*Bearer\s+[^,\s]+", re.IGNORECASE),
    re.compile(r"Bearer\s+[^,\s]+", re.IGNORECASE),
]


@dataclass(slots=True)
class ParsedSentence:
    """Rule-based verifier가 사용할 sentence/citation parser result item."""

    sentence_id: str
    text: str
    citations: list[str]
    matched_context_ids: list[str]
    invalid_citations: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _redact_payload(to_primitive(self))


@dataclass(slots=True)
class SentenceCitationParseResult:
    """Sentence/citation parser result."""

    sentences: list[ParsedSentence]
    citation_coverage: CitationCoverage
    warnings: list[WarningItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _redact_payload(
            {
                "sentences": [sentence.to_dict() for sentence in self.sentences],
                "citation_coverage": self.citation_coverage.to_dict(),
                "warnings": [warning.to_dict() for warning in self.warnings],
            }
        )


def parse_sentences_and_citations(
    normalized_input: NormalizedVerificationInput,
) -> SentenceCitationParseResult:
    """Normalized answer output에서 verification sentence와 citation coverage를 만든다."""
    warnings = list(normalized_input.warnings)
    valid_context_ids = {context.context_id for context in normalized_input.contexts}

    if normalized_input.answer_output.sentences:
        raw_sentences = normalized_input.answer_output.sentences
        sentences = [
            _parse_generated_sentence(index, raw_sentence, valid_context_ids, warnings)
            for index, raw_sentence in enumerate(raw_sentences, start=1)
        ]
    else:
        warnings.append(
            WarningItem(
                code="fallback_sentence_parsing_used",
                message="Generated sentences are empty; parsed sentences from answer text.",
            )
        )
        sentences = _parse_answer_text(
            normalized_input.answer_output.answer,
            valid_context_ids,
            warnings,
        )

    if not sentences:
        warnings.append(
            WarningItem(
                code="sentence_parse_empty",
                message="No verification sentences could be parsed from answer output.",
            )
        )

    return SentenceCitationParseResult(
        sentences=sentences,
        citation_coverage=_calculate_citation_coverage(sentences),
        warnings=warnings,
    )


def _parse_generated_sentence(
    index: int,
    raw_sentence: dict[str, Any],
    valid_context_ids: set[str],
    warnings: list[WarningItem],
) -> ParsedSentence:
    sentence_id = _normalize_text(str(raw_sentence.get("sentence_id") or "")) or f"s{index}"
    text = _normalize_text(str(raw_sentence.get("text") or ""))
    citations = _normalize_citations(raw_sentence.get("citations"))
    matched_context_ids, invalid_citations = _split_citations(
        sentence_id,
        citations,
        valid_context_ids,
        warnings,
    )
    return ParsedSentence(
        sentence_id=sentence_id,
        text=text,
        citations=citations,
        matched_context_ids=matched_context_ids,
        invalid_citations=invalid_citations,
        metadata={
            key: _redact_payload(value)
            for key, value in raw_sentence.items()
            if key not in {"sentence_id", "text", "citations"}
        },
    )


def _parse_answer_text(
    answer: str,
    valid_context_ids: set[str],
    warnings: list[WarningItem],
) -> list[ParsedSentence]:
    normalized_answer = _normalize_text(answer)
    if not normalized_answer:
        return []
    parts = [
        _normalize_text(match.group(0))
        for match in re.finditer(r"[^.!?。！？]+[.!?。！？]?", normalized_answer)
    ]
    sentences: list[ParsedSentence] = []
    for index, text in enumerate([part for part in parts if part], start=1):
        matched_context_ids, invalid_citations = _split_citations(
            f"s{index}",
            [],
            valid_context_ids,
            warnings,
        )
        sentences.append(
            ParsedSentence(
                sentence_id=f"s{index}",
                text=text,
                citations=[],
                matched_context_ids=matched_context_ids,
                invalid_citations=invalid_citations,
                metadata={"fallback_parsed": True},
            )
        )
    return sentences


def _split_citations(
    sentence_id: str,
    citations: list[str],
    valid_context_ids: set[str],
    warnings: list[WarningItem],
) -> tuple[list[str], list[str]]:
    matched_context_ids: list[str] = []
    invalid_citations: list[str] = []
    for citation in citations:
        if citation in valid_context_ids:
            matched_context_ids.append(citation)
        else:
            invalid_citations.append(citation)
            warnings.append(
                WarningItem(
                    code="invalid_citation",
                    message=f"Sentence {sentence_id} references unknown context_id {citation}.",
                )
            )
    return matched_context_ids, invalid_citations


def _calculate_citation_coverage(
    sentences: list[ParsedSentence],
) -> CitationCoverage:
    total_sentences = len(sentences)
    sentences_with_citations = sum(1 for sentence in sentences if sentence.citations)
    valid_citations = sum(len(sentence.matched_context_ids) for sentence in sentences)
    invalid_citations = sum(len(sentence.invalid_citations) for sentence in sentences)
    sentences_with_valid_citations = sum(
        1 for sentence in sentences if sentence.matched_context_ids
    )
    coverage_ratio = (
        sentences_with_valid_citations / total_sentences
        if total_sentences
        else 0.0
    )
    return CitationCoverage(
        total_sentences=total_sentences,
        sentences_with_citations=sentences_with_citations,
        valid_citations=valid_citations,
        invalid_citations=invalid_citations,
        coverage_ratio=coverage_ratio,
    )


def _normalize_citations(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    citations: list[str] = []
    seen: set[str] = set()
    for item in value:
        citation = _normalize_text(str(item or ""))
        if not citation or citation in seen:
            continue
        citations.append(citation)
        seen.add(citation)
    return citations


def _normalize_text(value: str) -> str:
    return _redact_text(re.sub(r"\s+", " ", value).strip())


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
