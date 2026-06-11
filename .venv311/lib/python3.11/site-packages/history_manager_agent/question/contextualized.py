from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : History Manager Agent contextualized question 및 Query Routing input 생성.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature5 contextualized question 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/typing 기반
--------------------------------------------------
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from history_manager_agent.context import ContextPolicyResult
from history_manager_agent.schemas import (
    HistoryDecision,
    HistoryDecisionLabel,
    PreservedContext,
    QueryRoutingInput,
)


class ContextualizedQuestionProvider(Protocol):
    """선택적 question rewriting provider interface."""

    def rewrite_question(self, request: "ContextualizedQuestionRequest") -> str:
        """독립 질문 후보를 반환한다."""


@dataclass(slots=True)
class ContextualizedQuestionRequest:
    """Question rewriter에 전달하는 safe request."""

    original_question: str
    policy_result: ContextPolicyResult

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "original_question": self.original_question,
            "history_decision": self.policy_result.history_decision.value,
            "reset_required": self.policy_result.reset_required,
            "preserved_context": self.policy_result.preserved_context.to_dict(),
        }


@dataclass(slots=True)
class ContextualizedQuestionResult:
    """Contextualized question 생성 결과."""

    conversation_id: str
    user_id: str
    original_question: str
    contextualized_question: str
    history_decision: HistoryDecisionLabel
    reset_required: bool
    confidence: float
    reason: str
    preserved_context: PreservedContext
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.history_decision = HistoryDecisionLabel(self.history_decision)
        self.preserved_context.validate()
        if not self.original_question:
            raise ValueError("original_question is required")
        if not self.contextualized_question:
            raise ValueError("contextualized_question is required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if not self.reason:
            raise ValueError("reason is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "original_question": self.original_question,
            "contextualized_question": self.contextualized_question,
            "history_decision": self.history_decision.value,
            "reset_required": self.reset_required,
            "confidence": self.confidence,
            "reason": self.reason,
            "preserved_context": self.preserved_context.to_dict(),
            "warnings": list(self.warnings),
            "metadata": dict(self.metadata),
        }


class FakeQuestionRewriter:
    """기본 테스트용 deterministic fake rewriter."""

    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.requests: list[ContextualizedQuestionRequest] = []

    def rewrite_question(self, request: ContextualizedQuestionRequest) -> str:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def build_question_result(
    conversation_id: str,
    user_id: str,
    current_question: str,
    policy_result: ContextPolicyResult,
    rewriter: ContextualizedQuestionProvider | None = None,
    max_question_chars: int = 500,
    metadata: dict[str, Any] | None = None,
) -> ContextualizedQuestionResult:
    """policy 결과를 기반으로 contextualized question을 생성한다."""
    original_question = _normalize_space(current_question)
    if not original_question:
        raise ValueError("current_question is required")

    warnings = list(policy_result.warnings)
    candidate = _question_candidate(
        original_question=original_question,
        policy_result=policy_result,
        rewriter=rewriter,
        warnings=warnings,
    )
    contextualized_question = _validate_or_fallback_question(
        candidate=candidate,
        original_question=original_question,
        max_question_chars=max_question_chars,
        warnings=warnings,
    )

    return ContextualizedQuestionResult(
        conversation_id=conversation_id,
        user_id=user_id,
        original_question=original_question,
        contextualized_question=contextualized_question,
        history_decision=policy_result.history_decision,
        reset_required=policy_result.reset_required,
        confidence=policy_result.confidence,
        reason=policy_result.reason,
        preserved_context=policy_result.preserved_context,
        warnings=_deduplicate(warnings),
        metadata=_safe_metadata(metadata or {}),
    )


def build_history_decision(
    question_result: ContextualizedQuestionResult,
) -> HistoryDecision:
    """History Manager canonical decision schema를 생성한다."""
    return HistoryDecision(
        conversation_id=question_result.conversation_id,
        user_id=question_result.user_id,
        original_question=question_result.original_question,
        contextualized_question=question_result.contextualized_question,
        history_decision=question_result.history_decision,
        reset_required=question_result.reset_required,
        confidence=question_result.confidence,
        reason=question_result.reason,
        preserved_context=question_result.preserved_context,
        warnings=question_result.warnings,
    )


def build_query_routing_input(
    question_result: ContextualizedQuestionResult,
    metadata: dict[str, Any] | None = None,
) -> QueryRoutingInput:
    """Query Routing Agent 입력과 호환되는 payload를 생성한다."""
    merged_metadata = dict(question_result.metadata)
    merged_metadata.update(metadata or {})
    return QueryRoutingInput(
        conversation_id=question_result.conversation_id,
        user_id=question_result.user_id,
        original_question=question_result.original_question,
        query=question_result.contextualized_question,
        history_decision=question_result.history_decision,
        preserved_context=question_result.preserved_context,
        reset_required=question_result.reset_required,
        metadata=_safe_metadata(merged_metadata),
    )


def _question_candidate(
    original_question: str,
    policy_result: ContextPolicyResult,
    rewriter: ContextualizedQuestionProvider | None,
    warnings: list[str],
) -> str:
    if policy_result.history_decision == HistoryDecisionLabel.NEW_TOPIC:
        return original_question

    if policy_result.history_decision == HistoryDecisionLabel.AMBIGUOUS:
        warnings.append("ambiguous_conservative_question")
        return original_question

    request = ContextualizedQuestionRequest(
        original_question=original_question,
        policy_result=policy_result,
    )
    if rewriter is not None:
        try:
            return _normalize_space(rewriter.rewrite_question(request))
        except Exception:
            warnings.append("question_rewrite_failed")
            return original_question

    summary = _normalize_space(policy_result.preserved_context.summary)
    if not summary:
        return original_question
    return f"{summary}에서 {original_question}"


def _validate_or_fallback_question(
    candidate: str,
    original_question: str,
    max_question_chars: int,
    warnings: list[str],
) -> str:
    if max_question_chars <= 0:
        raise ValueError("max_question_chars must be greater than 0")
    normalized_candidate = _normalize_space(candidate)
    if not normalized_candidate:
        warnings.append("question_rewrite_empty")
        return _fit_question(original_question, max_question_chars, warnings)
    if len(normalized_candidate) > max_question_chars:
        warnings.append("question_rewrite_too_long")
        return _fit_question(original_question, max_question_chars, warnings)
    return normalized_candidate


def _fit_question(
    original_question: str,
    max_question_chars: int,
    warnings: list[str],
) -> str:
    if len(original_question) <= max_question_chars:
        return original_question
    warnings.append("original_question_truncated")
    if max_question_chars <= 3:
        return original_question[:max_question_chars]
    return original_question[: max_question_chars - 3].rstrip() + "..."


def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    blocked_keys = {"history", "raw_history", "full_history", "messages"}
    safe_metadata: dict[str, Any] = {}
    for key, value in metadata.items():
        if key in blocked_keys:
            continue
        safe_metadata[key] = value
    return safe_metadata


def _normalize_space(value: str) -> str:
    return " ".join(str(value).split())


def _deduplicate(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
