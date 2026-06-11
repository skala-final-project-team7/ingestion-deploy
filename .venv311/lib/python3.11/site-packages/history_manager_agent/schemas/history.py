from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : History Manager Agent canonical schema 정의.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature1 schema 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/enum 기반
--------------------------------------------------
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from history_manager_agent.schemas._serialization import to_primitive


class ConversationRole(StrEnum):
    """MVP에서 허용하는 conversation turn role."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class HistoryDecisionLabel(StrEnum):
    """History decision label.

    from_value()는 후속 label 확장 시 unknown-safe 처리를 돕는다.
    """

    FOLLOW_UP = "follow_up"
    NEW_TOPIC = "new_topic"
    AMBIGUOUS = "ambiguous"

    @classmethod
    def from_value(cls, value: str) -> "HistoryDecisionLabel | str":
        """알려진 label은 enum으로, unknown label은 원문 문자열로 반환한다."""
        try:
            return cls(value)
        except ValueError:
            if not value:
                raise ValueError("history_decision is required") from None
            return value


class HistoryReportStatus(StrEnum):
    """History Manager job report status."""

    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


@dataclass(slots=True)
class ConversationTurn:
    """BFF가 전달하는 conversation history turn schema."""

    turn_id: str
    role: ConversationRole
    content: str
    created_at: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.role = ConversationRole(self.role)
        self.validate()

    def validate(self) -> None:
        """Conversation turn 필수값을 검증한다."""
        if not self.turn_id:
            raise ValueError("turn_id is required")
        if not self.content:
            raise ValueError("content is required")
        if not self.created_at:
            raise ValueError("created_at is required")
        if not isinstance(self.citations, list):
            raise ValueError("citations must be a list")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be an object")

    def to_dict(self) -> dict[str, Any]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class HistoryManagerInput:
    """History Manager Agent input schema."""

    conversation_id: str
    user_id: str
    current_question: str
    history: list[ConversationTurn] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.history = [
            turn if isinstance(turn, ConversationTurn) else _turn_from_dict(turn)
            for turn in self.history
        ]
        self.validate()

    def validate(self) -> None:
        """Input contract 필수값을 검증한다."""
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        if not self.user_id:
            raise ValueError("user_id is required")
        if not self.current_question:
            raise ValueError("current_question is required")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be an object")
        for turn in self.history:
            turn.validate()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "HistoryManagerInput":
        """primitive dict에서 HistoryManagerInput을 생성한다."""
        return cls(
            conversation_id=str(payload.get("conversation_id") or ""),
            user_id=str(payload.get("user_id") or ""),
            current_question=str(payload.get("current_question") or ""),
            history=[_turn_from_dict(item) for item in payload.get("history", [])],
            metadata=payload.get("metadata") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class PreservedContext:
    """Query Routing Agent가 사용할 preserved context schema."""

    summary: str = ""
    entities: list[str] = field(default_factory=list)
    turn_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Preserved context 필드 타입을 검증한다."""
        if not isinstance(self.summary, str):
            raise ValueError("summary must be a string")
        if not isinstance(self.entities, list):
            raise ValueError("entities must be a list")
        if not isinstance(self.turn_refs, list):
            raise ValueError("turn_refs must be a list")

    def to_dict(self) -> dict[str, Any]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class HistoryDecision:
    """History Manager Agent canonical output schema."""

    conversation_id: str
    user_id: str
    original_question: str
    contextualized_question: str
    history_decision: HistoryDecisionLabel | str
    reset_required: bool
    confidence: float
    reason: str
    preserved_context: PreservedContext
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.history_decision = _decision_label(self.history_decision)
        if not isinstance(self.preserved_context, PreservedContext):
            self.preserved_context = _preserved_context_from_dict(self.preserved_context)
        self.validate()

    def validate(self) -> None:
        """History decision 필수값과 confidence 범위를 검증한다."""
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        if not self.user_id:
            raise ValueError("user_id is required")
        if not self.original_question:
            raise ValueError("original_question is required")
        if not self.contextualized_question:
            raise ValueError("contextualized_question is required")
        if not self.history_decision:
            raise ValueError("history_decision is required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if not self.reason:
            raise ValueError("reason is required")
        if not isinstance(self.warnings, list):
            raise ValueError("warnings must be a list")
        self.preserved_context.validate()

    def to_dict(self) -> dict[str, Any]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class QueryRoutingInput:
    """Query Routing Agent 입력과 호환되는 schema."""

    conversation_id: str
    user_id: str
    original_question: str
    query: str
    history_decision: HistoryDecisionLabel | str
    preserved_context: PreservedContext
    reset_required: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.history_decision = _decision_label(self.history_decision)
        if not isinstance(self.preserved_context, PreservedContext):
            self.preserved_context = _preserved_context_from_dict(self.preserved_context)
        self.validate()

    def validate(self) -> None:
        """Query Routing input 필수값을 검증한다."""
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        if not self.user_id:
            raise ValueError("user_id is required")
        if not self.original_question:
            raise ValueError("original_question is required")
        if not self.query:
            raise ValueError("query is required")
        if not self.history_decision:
            raise ValueError("history_decision is required")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be an object")
        self.preserved_context.validate()

    def to_dict(self) -> dict[str, Any]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class HistoryWarning:
    """History processing warning schema."""

    code: str
    message: str
    turn_id: str | None = None

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("warning code is required")
        if not self.message:
            raise ValueError("warning message is required")

    def to_dict(self) -> dict[str, Any]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        return to_primitive(self)


@dataclass(slots=True)
class HistoryFailedItem:
    """History Manager failed item schema."""

    stage: str
    error_type: str
    error_message: str
    retryable: bool
    status: Literal["failed"] = "failed"

    def __post_init__(self) -> None:
        if not self.stage:
            raise ValueError("stage is required")
        if not self.error_type:
            raise ValueError("error_type is required")
        if not self.error_message:
            raise ValueError("error_message is required")
        if self.status != "failed":
            raise ValueError("status must be failed")

    def to_dict(self) -> dict[str, Any]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        return to_primitive(self)


@dataclass(slots=True)
class HistoryReport:
    """History Manager Agent job report schema."""

    job_id: str
    conversation_id: str
    status: HistoryReportStatus
    decision: HistoryDecisionLabel | str
    input_turn_count: int
    used_turn_count: int
    warnings_count: int
    created_at: str

    def __post_init__(self) -> None:
        self.status = HistoryReportStatus(self.status)
        self.decision = _decision_label(self.decision)
        self.validate()

    def validate(self) -> None:
        """Report 필수값과 count 범위를 검증한다."""
        if not self.job_id:
            raise ValueError("job_id is required")
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        if not self.decision:
            raise ValueError("decision is required")
        for field_name in ("input_turn_count", "used_turn_count", "warnings_count"):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be greater than or equal to 0")
        if not self.created_at:
            raise ValueError("created_at is required")

    def to_dict(self) -> dict[str, Any]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)


def _turn_from_dict(payload: Any) -> ConversationTurn:
    if not isinstance(payload, dict):
        raise ValueError("history turn must be an object")
    return ConversationTurn(
        turn_id=str(payload.get("turn_id") or ""),
        role=payload.get("role") or "",
        content=str(payload.get("content") or ""),
        created_at=str(payload.get("created_at") or ""),
        citations=payload.get("citations") or [],
        metadata=payload.get("metadata") or {},
    )


def _preserved_context_from_dict(payload: Any) -> PreservedContext:
    if not isinstance(payload, dict):
        raise ValueError("preserved_context must be an object")
    return PreservedContext(
        summary=str(payload.get("summary") or ""),
        entities=list(payload.get("entities") or []),
        turn_refs=list(payload.get("turn_refs") or []),
    )


def _decision_label(value: HistoryDecisionLabel | str) -> HistoryDecisionLabel | str:
    if isinstance(value, HistoryDecisionLabel):
        return value
    return HistoryDecisionLabel.from_value(str(value))
