from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Generation Agent sentence-level citation mapping 구현.
          LLM raw answer와 Top context를 검증 가능한 sentence/source 구조로 변환한다.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature5 citation mapping 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/re 기반
--------------------------------------------------
"""

import re
from dataclasses import dataclass, field
from typing import Any

from answer_generation_agent.generation.answer_generation import (
    AnswerGenerationResult,
    RawSentenceCandidate,
)
from answer_generation_agent.generation.input_normalization import (
    NormalizedGenerationInputResult,
)
from answer_generation_agent.schemas import (
    GeneratedSentence,
    GeneratedSource,
    TopContext,
    WarningItem,
)

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
class CitationMappingResult:
    """Feature5 citation mapping result."""

    sentences: list[GeneratedSentence]
    sources: list[GeneratedSource]
    used_context_ids: list[str]
    warnings: list[WarningItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.warnings = [
            warning
            if isinstance(warning, WarningItem)
            else WarningItem(
                code=str(warning.get("code") or "citation_mapping_warning"),
                message=str(warning.get("message") or "Citation mapping warning."),
            )
            for warning in self.warnings
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sentences": [sentence.to_dict() for sentence in self.sentences],
            "sources": [source.to_dict() for source in self.sources],
            "used_context_ids": list(self.used_context_ids),
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


def map_citations(
    *,
    generation_result: AnswerGenerationResult,
    normalized_input: NormalizedGenerationInputResult,
) -> CitationMappingResult:
    """LLM raw answer와 Top context에서 검증 가능한 sentence/source 구조를 만든다."""
    warnings = list(generation_result.warnings)
    context_by_id = {
        context.context_id: context for context in normalized_input.normalized_contexts
    }
    sentence_texts = _sentence_texts(generation_result)
    if not sentence_texts:
        warnings.append(
            WarningItem(
                code="empty_answer",
                message="Generated answer text is empty after normalization.",
            )
        )
        return CitationMappingResult(
            sentences=[],
            sources=[],
            used_context_ids=[],
            warnings=warnings,
        )

    sentences: list[GeneratedSentence] = []
    used_context_ids: list[str] = []
    for index, text in enumerate(sentence_texts, start=1):
        candidate = _candidate_for_sentence(
            index=index,
            text=text,
            candidates=generation_result.raw_sentence_candidates,
        )
        citations = _valid_citations(
            candidate.citations if candidate else [],
            context_by_id=context_by_id,
            warnings=warnings,
        )
        if not citations:
            citations = _fallback_citations(
                context_by_id=context_by_id,
                warnings=warnings,
            )
        for citation in citations:
            if citation not in used_context_ids:
                used_context_ids.append(citation)
        sentences.append(
            GeneratedSentence(
                sentence_id=f"s{index}",
                text=_redact_text(text),
                citations=citations,
                citation_required=True,
            )
        )

    sources = _build_sources(
        used_context_ids=used_context_ids,
        context_by_id=context_by_id,
    )
    return CitationMappingResult(
        sentences=sentences,
        sources=sources,
        used_context_ids=used_context_ids,
        warnings=warnings,
    )


def _sentence_texts(generation_result: AnswerGenerationResult) -> list[str]:
    candidate_texts = [
        _redact_text(candidate.text).strip()
        for candidate in generation_result.raw_sentence_candidates
        if candidate.text.strip()
    ]
    if candidate_texts:
        return candidate_texts
    answer_text = _redact_text(generation_result.answer_text).strip()
    if not answer_text:
        return []
    return [
        sentence.strip()
        for sentence in re.findall(r"[^.!?]+[.!?]|[^.!?]+$", answer_text)
        if sentence.strip()
    ]


def _candidate_for_sentence(
    *,
    index: int,
    text: str,
    candidates: list[RawSentenceCandidate],
) -> RawSentenceCandidate | None:
    if index <= len(candidates):
        return candidates[index - 1]
    normalized_text = _normalize_text(text)
    for candidate in candidates:
        if _normalize_text(candidate.text) == normalized_text:
            return candidate
    return None


def _valid_citations(
    citations: list[str],
    *,
    context_by_id: dict[str, TopContext],
    warnings: list[WarningItem],
) -> list[str]:
    valid: list[str] = []
    for citation in citations:
        safe_citation = _redact_text(str(citation))
        if safe_citation not in context_by_id:
            warnings.append(
                WarningItem(
                    code="invalid_citation_removed",
                    message="A citation that does not match a Top context id was removed.",
                )
            )
            continue
        if safe_citation not in valid:
            valid.append(safe_citation)
    return valid


def _fallback_citations(
    *,
    context_by_id: dict[str, TopContext],
    warnings: list[WarningItem],
) -> list[str]:
    if len(context_by_id) == 1:
        warnings.append(
            WarningItem(
                code="fallback_citation_applied",
                message="A single Top context was used as fallback citation.",
            )
        )
        return [next(iter(context_by_id))]
    warnings.append(
        WarningItem(
            code="missing_citation",
            message="A generated sentence has no reliable citation candidate.",
        )
    )
    return []


def _build_sources(
    *,
    used_context_ids: list[str],
    context_by_id: dict[str, TopContext],
) -> list[GeneratedSource]:
    sources: list[GeneratedSource] = []
    seen_context_ids: set[str] = set()
    for context_id in used_context_ids:
        if context_id in seen_context_ids or context_id not in context_by_id:
            continue
        context = context_by_id[context_id]
        seen_context_ids.add(context_id)
        sources.append(
            GeneratedSource(
                source_id=context.context_id,
                context_id=context.context_id,
                document_id=context.document_id,
                chunk_id=context.chunk_id,
                title=_redact_text(context.title),
                source_url=_redact_text(context.source_url),
                space_key=_redact_text(context.space_key),
                page_id=_redact_text(str(context.metadata.get("page_id") or "")),
                attachment_filename=_attachment_filename(context),
                score=context.score,
                rerank_score=context.rerank_score,
            )
        )
    return sources


def _attachment_filename(context: TopContext) -> str | None:
    value = context.metadata.get("attachment_filename")
    if value is None:
        return None
    return _redact_text(str(value))


def _normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


def _redact_text(text: str) -> str:
    redacted = text
    for marker in _REDACTION_MARKERS:
        redacted = redacted.replace(marker, "<redacted>")
    return redacted
