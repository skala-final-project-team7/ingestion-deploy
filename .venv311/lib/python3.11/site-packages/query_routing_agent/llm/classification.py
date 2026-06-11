from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Query Routing Agent intent classification service와 LLM output validation.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature3 intent classification 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/json 기반
--------------------------------------------------
"""

import json
from dataclasses import dataclass, field
from typing import Any

from query_routing_agent.config import QueryRoutingConfig
from query_routing_agent.routing import NormalizedRoutingInputResult
from query_routing_agent.schemas import IntentLabel, QueryRoutingInput, WarningItem

from .providers import (
    LLMProviderResponse,
    RoutingClassificationRequest,
    RoutingLLMProvider,
)


class ClassificationValidationError(ValueError):
    """LLM classification output이 schema와 맞지 않을 때 사용하는 safe error."""


@dataclass(slots=True)
class IntentClassificationResult:
    """Intent classification service result."""

    intent: IntentLabel
    confidence: float
    reason: str
    warnings: list[WarningItem] = field(default_factory=list)
    raw_hints: dict[str, Any] = field(default_factory=dict)

    def to_safe_dict(self) -> dict[str, Any]:
        """로그/report에 사용할 수 있는 primitive dictionary를 반환한다."""
        return {
            "intent": self.intent.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "warnings": [warning.to_dict() for warning in self.warnings],
            "raw_hints": self.raw_hints,
        }


def build_routing_prompt(routing_input: QueryRoutingInput) -> str:
    """Normalized routing input을 intent classification prompt로 변환한다."""
    context = routing_input.preserved_context
    entities = ", ".join(context.entities[:8])
    turn_refs = ", ".join(context.turn_refs[:8])
    return "\n".join(
        [
            "Classify the routing intent for a RAG search request.",
            f"query: {routing_input.query}",
            f"original_question: {routing_input.original_question}",
            f"decision: {routing_input.history_decision}",
            f"reset_required: {routing_input.reset_required}",
            f"context_summary: {context.summary[:1000]}",
            f"context_entities: {entities}",
            f"context_turn_refs: {turn_refs}",
            "Return JSON with intent, confidence, reason.",
        ]
    )


def classify_intent(
    normalized_input: NormalizedRoutingInputResult,
    config: QueryRoutingConfig,
    provider: RoutingLLMProvider,
) -> IntentClassificationResult:
    """Provider 응답을 검증해 intent classification result로 변환한다."""
    request = RoutingClassificationRequest(
        query=normalized_input.routing_input.query,
        prompt=build_routing_prompt(normalized_input.routing_input),
        routing_input=normalized_input.routing_input.to_dict(),
        model=config.model,
        temperature=config.temperature,
        timeout_seconds=config.timeout_seconds,
    )
    response = provider.route_query(request)
    return parse_routing_llm_response(response)


def parse_routing_llm_response(
    response: LLMProviderResponse,
) -> IntentClassificationResult:
    """Raw provider JSON response를 IntentClassificationResult로 검증/변환한다."""
    try:
        payload = json.loads(response.content)
    except json.JSONDecodeError as exc:
        raise ClassificationValidationError("Invalid LLM JSON response") from exc

    if not isinstance(payload, dict):
        raise ClassificationValidationError("LLM response must be an object")

    raw_intent = payload.get("intent")
    if not isinstance(raw_intent, str) or not raw_intent:
        raise ClassificationValidationError("intent is required")

    warnings: list[WarningItem] = []
    try:
        intent = IntentLabel(raw_intent)
    except ValueError:
        intent = IntentLabel.UNKNOWN
        warnings.append(
            WarningItem(
                code="invalid_intent_fallback",
                message="Invalid intent was replaced with unknown.",
            )
        )

    confidence = payload.get("confidence")
    if not isinstance(confidence, int | float):
        raise ClassificationValidationError("confidence is required")
    confidence_float = float(confidence)
    if not 0 <= confidence_float <= 1:
        raise ClassificationValidationError("confidence must be between 0 and 1")

    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason:
        raise ClassificationValidationError("reason is required")

    return IntentClassificationResult(
        intent=intent,
        confidence=confidence_float,
        reason=reason,
        warnings=warnings,
        raw_hints=_extract_safe_hints(payload),
    )


def _extract_safe_hints(payload: dict[str, Any]) -> dict[str, Any]:
    """feature4/5 확장을 위한 optional hint만 보존한다."""
    hints: dict[str, Any] = {}
    for key in ("expanded_queries", "metadata_filter_hints"):
        if key in payload:
            hints[key] = payload[key]
    return hints
