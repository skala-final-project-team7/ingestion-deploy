from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : History classification 결과에 따른 deterministic context preservation/reset policy 구현.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature4 context policy 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/re 기반
--------------------------------------------------
"""

import re
from dataclasses import dataclass, field
from typing import Any

from history_manager_agent.history import NormalizedHistoryResult
from history_manager_agent.llm import HistoryClassification
from history_manager_agent.schemas import (
    ConversationTurn,
    HistoryDecisionLabel,
    PreservedContext,
)


@dataclass(slots=True)
class ContextPolicyResult:
    """Context policy 결과.

    feature4는 preserved context/reset/warnings만 만든다. contextualized question과
    routing input 생성은 후속 feature에서 담당한다.
    """

    history_decision: HistoryDecisionLabel
    reset_required: bool
    preserved_context: PreservedContext
    confidence: float
    reason: str
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.history_decision = HistoryDecisionLabel(self.history_decision)
        self.preserved_context.validate()
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if not self.reason:
            raise ValueError("reason is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "history_decision": self.history_decision.value,
            "reset_required": self.reset_required,
            "preserved_context": self.preserved_context.to_dict(),
            "confidence": self.confidence,
            "reason": self.reason,
            "warnings": list(self.warnings),
        }


def apply_context_policy(
    normalized_history: NormalizedHistoryResult,
    classification: HistoryClassification,
    max_summary_chars: int = 500,
) -> ContextPolicyResult:
    """classification label에 따라 deterministic context policy를 적용한다."""
    warnings = _normalization_warning_codes(normalized_history)

    if classification.history_decision == HistoryDecisionLabel.NEW_TOPIC:
        warnings.append("context_reset")
        return ContextPolicyResult(
            history_decision=classification.history_decision,
            reset_required=True,
            preserved_context=PreservedContext(),
            confidence=classification.confidence,
            reason=classification.reason,
            warnings=warnings,
        )

    if classification.history_decision == HistoryDecisionLabel.AMBIGUOUS:
        selected_turns = normalized_history.to_llm_context_turns()[-2:]
        if classification.confidence < 0.5:
            warnings.append("ambiguous_low_confidence")
        preserved_context, context_warnings = _build_preserved_context(
            selected_turns,
            max_summary_chars,
        )
        return ContextPolicyResult(
            history_decision=classification.history_decision,
            reset_required=False,
            preserved_context=preserved_context,
            confidence=classification.confidence,
            reason=classification.reason,
            warnings=warnings + context_warnings,
        )

    selected_turns = normalized_history.to_llm_context_turns()
    preserved_context, context_warnings = _build_preserved_context(
        selected_turns,
        max_summary_chars,
    )
    return ContextPolicyResult(
        history_decision=classification.history_decision,
        reset_required=False,
        preserved_context=preserved_context,
        confidence=classification.confidence,
        reason=classification.reason,
        warnings=warnings + context_warnings,
    )


def _build_preserved_context(
    turns: list[ConversationTurn],
    max_summary_chars: int,
) -> tuple[PreservedContext, list[str]]:
    if not turns:
        return PreservedContext(), ["empty_history_context"]

    raw_summary = _summarize_turns(turns)
    summary, was_truncated = _truncate(raw_summary, max_summary_chars)
    warnings = ["context_summary_truncated"] if was_truncated else []
    return (
        PreservedContext(
            summary=summary,
            entities=_extract_entities(summary),
            turn_refs=[turn.turn_id for turn in turns],
        ),
        warnings,
    )


def _summarize_turns(turns: list[ConversationTurn]) -> str:
    parts = []
    for turn in turns:
        snippet, _ = _truncate(_normalize_space(turn.content), 120)
        parts.append(f"{turn.role.value}: {snippet}")
    return " | ".join(parts)


def _truncate(value: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0:
        raise ValueError("max_summary_chars must be greater than 0")
    if len(value) <= max_chars:
        return value, False
    if max_chars <= 3:
        return value[:max_chars], True
    return value[: max_chars - 3].rstrip() + "...", True


def _extract_entities(summary: str) -> list[str]:
    candidates = re.findall(r"\b[A-Z][A-Z0-9_-]{1,}\b", summary)
    seen: set[str] = set()
    entities: list[str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        entities.append(candidate)
    return entities


def _normalize_space(value: str) -> str:
    return " ".join(value.split())


def _normalization_warning_codes(
    normalized_history: NormalizedHistoryResult,
) -> list[str]:
    return [warning.code for warning in normalized_history.warnings]
