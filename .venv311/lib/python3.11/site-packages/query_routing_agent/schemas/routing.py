from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Query Routing Agent feature1 canonical schema 정의.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, routing input/decision/filter/search/report schema 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/enum 기반
--------------------------------------------------
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from query_routing_agent.schemas._serialization import to_primitive


class HistoryDecisionLabel(StrEnum):
    """History Manager Agent가 전달하는 history decision label."""

    FOLLOW_UP = "follow_up"
    NEW_TOPIC = "new_topic"
    AMBIGUOUS = "ambiguous"

    @classmethod
    def from_value(cls, value: str) -> "HistoryDecisionLabel | str":
        """알려진 label은 enum으로, unknown label은 확장값으로 반환한다."""
        try:
            return cls(value)
        except ValueError:
            if not value:
                raise ValueError("history_decision is required") from None
            return value


class IntentLabel(StrEnum):
    """MVP intent label."""

    INCIDENT_RESPONSE = "incident_response"
    OPERATIONS_GUIDE = "operations_guide"
    POLICY_PROCEDURE = "policy_procedure"
    HISTORY_LOOKUP = "history_lookup"
    UNKNOWN = "unknown"

    @classmethod
    def from_value(cls, value: str) -> "IntentLabel | str":
        """알려진 intent는 enum으로, unknown extension은 원문 문자열로 반환한다."""
        try:
            return cls(value)
        except ValueError:
            if not value:
                raise ValueError("intent is required") from None
            return value


class TaskPromptType(StrEnum):
    """Answer Generation Agent가 사용할 task prompt type."""

    TIMELINE = "timeline"
    STEP_BY_STEP = "step_by_step"
    EVIDENCE_FIRST = "evidence_first"
    HISTORY_SUMMARY = "history_summary"
    GENERAL = "general"

    @classmethod
    def from_value(cls, value: str) -> "TaskPromptType | str":
        """알려진 prompt type은 enum으로, unknown extension은 원문 문자열로 반환한다."""
        try:
            return cls(value)
        except ValueError:
            if not value:
                raise ValueError("task_prompt_type is required") from None
            return value


class RoutingReportStatus(StrEnum):
    """Query Routing job report status."""

    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


@dataclass(slots=True)
class PreservedContext:
    """History Manager output의 preserved context schema."""

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
class QueryRoutingInput:
    """History Manager Agent output과 호환되는 Query Routing input schema."""

    conversation_id: str
    user_id: str
    original_question: str
    query: str
    history_decision: HistoryDecisionLabel | str
    preserved_context: PreservedContext
    reset_required: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.history_decision = _history_decision_label(self.history_decision)
        if not isinstance(self.preserved_context, PreservedContext):
            self.preserved_context = _preserved_context_from_dict(
                self.preserved_context
            )
        self.validate()

    def validate(self) -> None:
        """Input contract 필수값을 검증한다."""
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
        if not isinstance(self.reset_required, bool):
            raise ValueError("reset_required must be a boolean")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be an object")
        self.preserved_context.validate()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "QueryRoutingInput":
        """primitive dict에서 QueryRoutingInput을 생성한다."""
        return cls(
            conversation_id=str(payload.get("conversation_id") or ""),
            user_id=str(payload.get("user_id") or ""),
            original_question=str(payload.get("original_question") or ""),
            query=str(payload.get("query") or ""),
            history_decision=str(payload.get("history_decision") or ""),
            preserved_context=payload.get("preserved_context") or {},
            reset_required=bool(payload.get("reset_required", False)),
            metadata=payload.get("metadata") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class DateRangeFilter:
    """Canonical metadata date range filter."""

    from_date: str | None = None
    to_date: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """명세의 `from`/`to` key를 가진 primitive dictionary를 반환한다."""
        return {"from": self.from_date, "to": self.to_date}


@dataclass(slots=True)
class AclFilter:
    """ACL 전달 payload. 권한 판정 결과는 포함하지 않는다."""

    user_id: str = ""
    groups: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """ACL filter 필드 타입을 검증한다."""
        if not isinstance(self.user_id, str):
            raise ValueError("acl.user_id must be a string")
        if not isinstance(self.groups, list):
            raise ValueError("acl.groups must be a list")

    def to_dict(self) -> dict[str, Any]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return {"user_id": self.user_id, "groups": list(self.groups)}


@dataclass(slots=True)
class MetadataFilter:
    """중립적인 canonical metadata filter schema."""

    space_keys: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    document_types: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    date_range: DateRangeFilter = field(default_factory=DateRangeFilter)
    attachment_required: bool = False
    acl: AclFilter = field(default_factory=AclFilter)

    def __post_init__(self) -> None:
        if not isinstance(self.date_range, DateRangeFilter):
            self.date_range = _date_range_from_dict(self.date_range)
        if not isinstance(self.acl, AclFilter):
            self.acl = _acl_from_dict(self.acl)
        self.validate()

    def validate(self) -> None:
        """Metadata filter 필드 타입을 검증한다."""
        for field_name in (
            "space_keys",
            "labels",
            "document_types",
            "source_types",
        ):
            if not isinstance(getattr(self, field_name), list):
                raise ValueError(f"{field_name} must be a list")
        if not isinstance(self.attachment_required, bool):
            raise ValueError("attachment_required must be a boolean")
        self.acl.validate()

    def to_dict(self) -> dict[str, Any]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return {
            "space_keys": list(self.space_keys),
            "labels": list(self.labels),
            "document_types": list(self.document_types),
            "source_types": list(self.source_types),
            "date_range": self.date_range.to_dict(),
            "attachment_required": self.attachment_required,
            "acl": self.acl.to_dict(),
        }


@dataclass(slots=True)
class PoolWeights:
    """Multi-Pool 검색 weight schema."""

    title: float = 0.25
    content: float = 0.6
    label: float = 0.15

    def __post_init__(self) -> None:
        self.validate()

    @property
    def total(self) -> float:
        """Weight 합계를 반환한다."""
        return self.title + self.content + self.label

    def validate(self) -> None:
        """Weight 음수 여부와 합계 1.0 정책을 검증한다."""
        if self.title < 0 or self.content < 0 or self.label < 0:
            raise ValueError("pool_weights cannot be negative")
        if abs(self.total - 1.0) > 0.000001:
            raise ValueError("pool_weights must sum to 1.0")

    def to_dict(self) -> dict[str, float]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return {"title": self.title, "content": self.content, "label": self.label}


@dataclass(slots=True)
class WarningItem:
    """Routing process warning schema."""

    code: str
    message: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Warning 필수값을 검증한다."""
        if not self.code:
            raise ValueError("warning code is required")
        if not self.message:
            raise ValueError("warning message is required")

    def to_dict(self) -> dict[str, str]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return {"code": self.code, "message": self.message}


@dataclass(slots=True)
class RoutingDecision:
    """Query Routing Agent canonical output schema."""

    routing_id: str
    conversation_id: str
    user_id: str
    original_question: str
    query: str
    intent: IntentLabel | str
    task_prompt_type: TaskPromptType | str
    expanded_queries: list[str]
    metadata_filters: MetadataFilter
    pool_weights: PoolWeights
    confidence: float
    reason: str
    warnings: list[WarningItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.intent = _intent_label(self.intent)
        self.task_prompt_type = _task_prompt_type(self.task_prompt_type)
        if not isinstance(self.metadata_filters, MetadataFilter):
            self.metadata_filters = _metadata_filter_from_dict(self.metadata_filters)
        if not isinstance(self.pool_weights, PoolWeights):
            self.pool_weights = _pool_weights_from_dict(self.pool_weights)
        self.warnings = [
            warning
            if isinstance(warning, WarningItem)
            else _warning_item_from_dict(warning)
            for warning in self.warnings
        ]
        self.validate()

    def validate(self) -> None:
        """Routing decision 필수값과 confidence 범위를 검증한다."""
        if not self.routing_id:
            raise ValueError("routing_id is required")
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        if not self.user_id:
            raise ValueError("user_id is required")
        if not self.original_question:
            raise ValueError("original_question is required")
        if not self.query:
            raise ValueError("query is required")
        if not self.intent:
            raise ValueError("intent is required")
        if not self.task_prompt_type:
            raise ValueError("task_prompt_type is required")
        if not self.expanded_queries:
            raise ValueError("expanded_queries is required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if not self.reason:
            raise ValueError("reason is required")
        self.metadata_filters.validate()
        self.pool_weights.validate()
        for warning in self.warnings:
            warning.validate()

    def to_dict(self) -> dict[str, Any]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return {
            "routing_id": self.routing_id,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "original_question": self.original_question,
            "query": self.query,
            "intent": _enum_or_str(self.intent),
            "task_prompt_type": _enum_or_str(self.task_prompt_type),
            "expanded_queries": list(self.expanded_queries),
            "metadata_filters": self.metadata_filters.to_dict(),
            "pool_weights": self.pool_weights.to_dict(),
            "confidence": self.confidence,
            "reason": self.reason,
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


@dataclass(slots=True)
class SearchRequestPayload:
    """RAG 검색 파이프라인으로 전달할 search request payload."""

    routing_id: str
    conversation_id: str
    user_id: str
    queries: list[str]
    filters: MetadataFilter
    pool_weights: PoolWeights
    top_k_candidates: int = 20
    rerank_top_k: int = 5
    reranking_required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.filters, MetadataFilter):
            self.filters = _metadata_filter_from_dict(self.filters)
        if not isinstance(self.pool_weights, PoolWeights):
            self.pool_weights = _pool_weights_from_dict(self.pool_weights)
        self.validate()

    def validate(self) -> None:
        """Search request payload 필수값을 검증한다."""
        if not self.routing_id:
            raise ValueError("routing_id is required")
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        if not self.user_id:
            raise ValueError("user_id is required")
        if not self.queries:
            raise ValueError("queries is required")
        if self.top_k_candidates <= 0:
            raise ValueError("top_k_candidates must be greater than 0")
        if self.rerank_top_k <= 0:
            raise ValueError("rerank_top_k must be greater than 0")
        if not isinstance(self.reranking_required, bool):
            raise ValueError("reranking_required must be a boolean")
        self.filters.validate()
        self.pool_weights.validate()

    def to_dict(self) -> dict[str, Any]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return {
            "routing_id": self.routing_id,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "queries": list(self.queries),
            "filters": self.filters.to_dict(),
            "pool_weights": self.pool_weights.to_dict(),
            "top_k_candidates": self.top_k_candidates,
            "rerank_top_k": self.rerank_top_k,
            "reranking_required": self.reranking_required,
        }


@dataclass(slots=True)
class RoutingReport:
    """Query Routing Agent job report schema."""

    job_id: str
    routing_id: str
    conversation_id: str
    status: RoutingReportStatus
    intent: IntentLabel | str
    expanded_query_count: int
    warnings_count: int
    created_at: str

    def __post_init__(self) -> None:
        self.status = RoutingReportStatus(self.status)
        self.intent = _intent_label(self.intent)
        self.validate()

    def validate(self) -> None:
        """Report 필수값을 검증한다."""
        if not self.job_id:
            raise ValueError("job_id is required")
        if not self.routing_id:
            raise ValueError("routing_id is required")
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        if self.expanded_query_count < 0:
            raise ValueError("expanded_query_count must be greater than or equal to 0")
        if self.warnings_count < 0:
            raise ValueError("warnings_count must be greater than or equal to 0")
        if not self.created_at:
            raise ValueError("created_at is required")

    def to_dict(self) -> dict[str, Any]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return {
            "job_id": self.job_id,
            "routing_id": self.routing_id,
            "conversation_id": self.conversation_id,
            "status": self.status.value,
            "intent": _enum_or_str(self.intent),
            "expanded_query_count": self.expanded_query_count,
            "warnings_count": self.warnings_count,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class FailedItem:
    """Safe failed item schema."""

    item_id: str
    reason: str
    retryable: bool
    error_type: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Failed item 필수값을 검증한다."""
        if not self.item_id:
            raise ValueError("item_id is required")
        if not self.reason:
            raise ValueError("reason is required")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be a boolean")
        if not self.error_type:
            raise ValueError("error_type is required")

    def to_dict(self) -> dict[str, Any]:
        """JSON output에 사용할 primitive dictionary를 반환한다."""
        self.validate()
        return to_primitive(self)


def _enum_or_str(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    return value


def _history_decision_label(value: HistoryDecisionLabel | str) -> HistoryDecisionLabel | str:
    return HistoryDecisionLabel.from_value(value.value if isinstance(value, StrEnum) else str(value))


def _intent_label(value: IntentLabel | str) -> IntentLabel | str:
    return IntentLabel.from_value(value.value if isinstance(value, StrEnum) else str(value))


def _task_prompt_type(value: TaskPromptType | str) -> TaskPromptType | str:
    return TaskPromptType.from_value(value.value if isinstance(value, StrEnum) else str(value))


def _preserved_context_from_dict(payload: Any) -> PreservedContext:
    if not isinstance(payload, dict):
        raise ValueError("preserved_context must be an object")
    return PreservedContext(
        summary=str(payload.get("summary") or ""),
        entities=list(payload.get("entities") or []),
        turn_refs=list(payload.get("turn_refs") or []),
    )


def _date_range_from_dict(payload: Any) -> DateRangeFilter:
    if not isinstance(payload, dict):
        raise ValueError("date_range must be an object")
    return DateRangeFilter(
        from_date=payload.get("from") or payload.get("from_date"),
        to_date=payload.get("to") or payload.get("to_date"),
    )


def _acl_from_dict(payload: Any) -> AclFilter:
    if not isinstance(payload, dict):
        raise ValueError("acl must be an object")
    return AclFilter(
        user_id=str(payload.get("user_id") or ""),
        groups=list(payload.get("groups") or []),
    )


def _metadata_filter_from_dict(payload: Any) -> MetadataFilter:
    if not isinstance(payload, dict):
        raise ValueError("metadata_filters must be an object")
    return MetadataFilter(
        space_keys=list(payload.get("space_keys") or []),
        labels=list(payload.get("labels") or []),
        document_types=list(payload.get("document_types") or []),
        source_types=list(payload.get("source_types") or []),
        date_range=payload.get("date_range") or {},
        attachment_required=bool(payload.get("attachment_required", False)),
        acl=payload.get("acl") or {},
    )


def _pool_weights_from_dict(payload: Any) -> PoolWeights:
    if not isinstance(payload, dict):
        raise ValueError("pool_weights must be an object")
    return PoolWeights(
        title=float(payload.get("title", 0.25)),
        content=float(payload.get("content", 0.6)),
        label=float(payload.get("label", 0.15)),
    )


def _warning_item_from_dict(payload: Any) -> WarningItem:
    if not isinstance(payload, dict):
        raise ValueError("warning must be an object")
    return WarningItem(
        code=str(payload.get("code") or ""),
        message=str(payload.get("message") or ""),
    )
