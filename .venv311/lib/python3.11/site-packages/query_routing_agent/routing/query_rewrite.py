from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Query Routing Agent feature4 query rewrite service 구현.
          LLM hint와 preserved context를 검색용 expanded query 목록으로 정규화한다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature4 query rewrite 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/re 기반
--------------------------------------------------
"""

import re
from dataclasses import dataclass, field
from typing import Any

from query_routing_agent.config import QueryRoutingConfig
from query_routing_agent.schemas import IntentLabel, WarningItem

from .normalization import NormalizedRoutingInputResult

MAX_QUERY_CHARS = 180
SENSITIVE_MARKERS = (
    "OPENAI_API_KEY",
    "Authorization",
    "Bearer",
    "api_key",
    "api key",
    "access_token",
    "token",
    "secret",
)


@dataclass(slots=True)
class QueryRewriteResult:
    """Query rewrite service result."""

    expanded_queries: list[str]
    warnings: list[WarningItem] = field(default_factory=list)

    def to_safe_dict(self) -> dict[str, Any]:
        """로그/report에 사용할 수 있는 primitive dictionary를 반환한다."""
        return {
            "expanded_queries": list(self.expanded_queries),
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


def rewrite_queries(
    normalized_input: NormalizedRoutingInputResult,
    classification: Any,
    config: QueryRoutingConfig,
) -> QueryRewriteResult:
    """LLM hint와 deterministic fallback을 사용해 expanded query를 생성한다."""
    warnings: list[WarningItem] = []
    candidates = _expanded_query_candidates(classification.raw_hints, warnings)
    used_fallback = False
    if not candidates:
        used_fallback = True
        candidates = _deterministic_candidates(normalized_input, classification.intent)

    normalized_queries = _normalize_queries(candidates, warnings)
    if not normalized_queries:
        used_fallback = True
        normalized_queries = _normalize_queries(
            [_safe_query_text(normalized_input.routing_input.query) or "redacted query"],
            warnings,
        )
    if used_fallback:
        warnings.append(
            WarningItem(
                code="expanded_queries_fallback",
                message="Expanded query hints were missing or invalid; deterministic fallback was used.",
            )
        )

    target_count = config.default_query_count
    fallback_candidates = _deterministic_candidates(normalized_input, classification.intent)
    fallback_index = 0
    padding_index = 1
    while len(normalized_queries) < target_count:
        if fallback_index < len(fallback_candidates):
            candidate = fallback_candidates[fallback_index]
            fallback_index += 1
        else:
            candidate = f"{_safe_base_query(normalized_input)} 검색 {padding_index}"
            padding_index += 1
        candidate = _limit_query_length(_safe_query_text(candidate), warnings)
        if candidate and candidate not in normalized_queries:
            normalized_queries.append(candidate)

    if len(normalized_queries) > config.max_query_count:
        normalized_queries = normalized_queries[: config.max_query_count]
        warnings.append(
            WarningItem(
                code="expanded_queries_trimmed",
                message="Expanded queries exceeded max_query_count and were trimmed.",
            )
        )

    return QueryRewriteResult(
        expanded_queries=normalized_queries,
        warnings=warnings,
    )


def _expanded_query_candidates(
    raw_hints: dict[str, Any],
    warnings: list[WarningItem],
) -> list[Any]:
    value = raw_hints.get("expanded_queries")
    if value is None:
        return []
    if not isinstance(value, list):
        warnings.append(
            WarningItem(
                code="expanded_queries_invalid",
                message="Expanded query hints were not a list and were ignored.",
            )
        )
        return []
    return value


def _normalize_queries(
    candidates: list[Any],
    warnings: list[WarningItem],
) -> list[str]:
    normalized: list[str] = []
    was_normalized = False
    for candidate in candidates:
        if not isinstance(candidate, str):
            was_normalized = True
            continue
        query = _safe_query_text(candidate)
        if not query:
            was_normalized = True
            continue
        limited_query = _limit_query_length(query, warnings)
        if limited_query != candidate.strip():
            was_normalized = True
        normalized.append(limited_query)

    deduped = _dedupe_preserving_order(normalized)
    if len(deduped) != len(normalized):
        was_normalized = True
    if was_normalized:
        warnings.append(
            WarningItem(
                code="expanded_queries_normalized",
                message="Expanded queries were cleaned, deduplicated, or sanitized.",
            )
        )
    return deduped


def _deterministic_candidates(
    normalized_input: NormalizedRoutingInputResult,
    intent: IntentLabel,
) -> list[str]:
    base_query = _safe_base_query(normalized_input)
    context_terms = _context_terms(normalized_input)
    enriched_query = _combine_terms(base_query, context_terms)

    if intent == IntentLabel.INCIDENT_RESPONSE:
        return [
            enriched_query,
            _combine_terms(enriched_query, ["장애", "롤백", "대응"]),
            _combine_terms(enriched_query, ["troubleshooting"]),
        ]
    if intent == IntentLabel.OPERATIONS_GUIDE:
        return [
            enriched_query,
            _combine_terms(enriched_query, ["운영", "절차"]),
            _combine_terms(enriched_query, ["가이드"]),
        ]
    if intent == IntentLabel.POLICY_PROCEDURE:
        return [
            enriched_query,
            _combine_terms(enriched_query, ["정책", "절차"]),
            _combine_terms(enriched_query, ["근거"]),
        ]
    if intent == IntentLabel.HISTORY_LOOKUP:
        return [
            enriched_query,
            _combine_terms(enriched_query, ["이력", "변경"]),
            _combine_terms(enriched_query, ["날짜"]),
        ]
    return [
        base_query,
        enriched_query,
        _combine_terms(enriched_query, ["검색"]),
    ]


def _safe_base_query(normalized_input: NormalizedRoutingInputResult) -> str:
    return _safe_query_text(normalized_input.routing_input.query) or "redacted query"


def _context_terms(normalized_input: NormalizedRoutingInputResult) -> list[str]:
    context = normalized_input.routing_input.preserved_context
    terms: list[str] = []
    for entity in context.entities[:4]:
        safe_entity = _safe_query_text(entity)
        if safe_entity:
            terms.append(safe_entity)
    return terms


def _combine_terms(base_query: str, terms: list[str]) -> str:
    combined = base_query
    for term in terms:
        if term and term.lower() not in combined.lower():
            combined = f"{combined} {term}"
    return combined


def _safe_query_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return ""
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in SENSITIVE_MARKERS):
        return ""
    return text


def _limit_query_length(query: str, warnings: list[WarningItem]) -> str:
    if len(query) <= MAX_QUERY_CHARS:
        return query
    warnings.append(
        WarningItem(
            code="expanded_query_truncated",
            message="Expanded query exceeded the maximum length and was truncated.",
        )
    )
    return query[:MAX_QUERY_CHARS].rstrip()


def _dedupe_preserving_order(queries: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for query in queries:
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(query)
    return deduped
