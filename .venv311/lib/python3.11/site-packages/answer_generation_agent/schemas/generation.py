from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Generation Agent feature1 canonical schema 정의.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, generation input/output/report schema 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/enum 기반
--------------------------------------------------
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from answer_generation_agent.schemas._serialization import to_primitive


class TaskPromptType(StrEnum):
    """Query Routing Agent가 전달하는 task prompt type."""

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


class AnswerStatus(StrEnum):
    """Answer output status."""

    SUCCESS = "success"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    FAILED = "failed"

    @classmethod
    def from_value(cls, value: str) -> "AnswerStatus | str":
        """알려진 status는 enum으로, unknown extension은 원문 문자열로 반환한다."""
        try:
            return cls(value)
        except ValueError:
            if not value:
                raise ValueError("answer_status is required") from None
            return value


class GenerationReportStatus(StrEnum):
    """Generation report status."""

    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class StreamChunkType(StrEnum):
    """후속 streaming adapter가 사용할 chunk type."""

    TEXT = "text"
    CITATION = "citation"
    DONE = "done"
    ERROR = "error"


@dataclass(slots=True)
class WarningItem:
    """Generation process warning schema."""

    code: str
    message: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.code:
            raise ValueError("warning code is required")
        if not self.message:
            raise ValueError("warning message is required")

    def to_dict(self) -> dict[str, str]:
        self.validate()
        return {"code": self.code, "message": self.message}


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
        if not self.item_id:
            raise ValueError("item_id is required")
        if not self.reason:
            raise ValueError("reason is required")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be a boolean")
        if not self.error_type:
            raise ValueError("error_type is required")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class RoutingDecisionInput:
    """Query Routing Agent output과 호환되는 routing decision 입력 schema."""

    routing_id: str
    original_question: str
    query: str
    intent: str
    task_prompt_type: TaskPromptType | str
    expanded_queries: list[str]
    metadata_filters: dict[str, Any]
    pool_weights: dict[str, Any]
    confidence: float = 0.0
    warnings: list[WarningItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.task_prompt_type = _task_prompt_type(self.task_prompt_type)
        self.warnings = [
            warning
            if isinstance(warning, WarningItem)
            else _warning_item_from_dict(warning)
            for warning in self.warnings
        ]
        self.validate()

    def validate(self) -> None:
        if not self.routing_id:
            raise ValueError("routing_id is required")
        if not self.query:
            raise ValueError("query is required")
        if not self.intent:
            raise ValueError("intent is required")
        if not self.task_prompt_type:
            raise ValueError("task_prompt_type is required")
        if not isinstance(self.expanded_queries, list):
            raise ValueError("expanded_queries must be a list")
        if not isinstance(self.metadata_filters, dict):
            raise ValueError("metadata_filters must be an object")
        if not isinstance(self.pool_weights, dict):
            raise ValueError("pool_weights must be an object")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        for warning in self.warnings:
            warning.validate()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RoutingDecisionInput":
        return cls(
            routing_id=str(payload.get("routing_id") or ""),
            original_question=str(payload.get("original_question") or ""),
            query=str(payload.get("query") or ""),
            intent=str(payload.get("intent") or ""),
            task_prompt_type=str(payload.get("task_prompt_type") or ""),
            expanded_queries=payload.get("expanded_queries") or [],
            metadata_filters=payload.get("metadata_filters") or {},
            pool_weights=payload.get("pool_weights") or {},
            confidence=float(payload.get("confidence", 0.0)),
            warnings=payload.get("warnings") or [],
        )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "routing_id": self.routing_id,
            "original_question": self.original_question,
            "query": self.query,
            "intent": self.intent,
            "task_prompt_type": _enum_or_str(self.task_prompt_type),
            "expanded_queries": list(self.expanded_queries),
            "metadata_filters": dict(self.metadata_filters),
            "pool_weights": dict(self.pool_weights),
            "confidence": self.confidence,
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


@dataclass(slots=True)
class TopContext:
    """RAG Pipeline/Cross-Encoder가 선별했다고 가정하는 Top context schema."""

    context_id: str
    document_id: str
    chunk_id: str
    title: str
    space_key: str
    source_url: str
    content: str
    score: float = 0.0
    rerank_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for field_name in (
            "context_id",
            "document_id",
            "chunk_id",
            "title",
            "space_key",
            "source_url",
            "content",
        ):
            if not getattr(self, field_name):
                raise ValueError(f"{field_name} is required")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be an object")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TopContext":
        return cls(
            context_id=str(payload.get("context_id") or ""),
            document_id=str(payload.get("document_id") or ""),
            chunk_id=str(payload.get("chunk_id") or ""),
            title=str(payload.get("title") or ""),
            space_key=str(payload.get("space_key") or ""),
            source_url=str(payload.get("source_url") or ""),
            content=str(payload.get("content") or ""),
            score=float(payload.get("score", 0.0)),
            rerank_score=float(payload.get("rerank_score", 0.0)),
            metadata=payload.get("metadata") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class SearchResults:
    """Top context 목록 wrapper."""

    top_contexts: list[TopContext] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.top_contexts = [
            context if isinstance(context, TopContext) else TopContext.from_dict(context)
            for context in self.top_contexts
        ]
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.top_contexts, list):
            raise ValueError("top_contexts must be a list")
        for context in self.top_contexts:
            context.validate()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SearchResults":
        return cls(top_contexts=payload.get("top_contexts") or [])

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {"top_contexts": [context.to_dict() for context in self.top_contexts]}


@dataclass(slots=True)
class GenerationInput:
    """Answer Generation Agent input schema."""

    conversation_id: str
    user_id: str
    routing_decision: RoutingDecisionInput
    search_results: SearchResults
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.routing_decision, RoutingDecisionInput):
            self.routing_decision = RoutingDecisionInput.from_dict(self.routing_decision)
        if not isinstance(self.search_results, SearchResults):
            self.search_results = SearchResults.from_dict(self.search_results)
        self.validate()

    def validate(self) -> None:
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        if not self.user_id:
            raise ValueError("user_id is required")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be an object")
        self.routing_decision.validate()
        self.search_results.validate()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GenerationInput":
        return cls(
            conversation_id=str(payload.get("conversation_id") or ""),
            user_id=str(payload.get("user_id") or ""),
            routing_decision=payload.get("routing_decision") or {},
            search_results=payload.get("search_results") or {},
            metadata=payload.get("metadata") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "routing_decision": self.routing_decision.to_dict(),
            "search_results": self.search_results.to_dict(),
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class GeneratedSentence:
    """Sentence-level answer with citation references."""

    sentence_id: str
    text: str
    citations: list[str]
    citation_required: bool = True

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.sentence_id:
            raise ValueError("sentence_id is required")
        if not self.text:
            raise ValueError("sentence text is required")
        if not isinstance(self.citations, list):
            raise ValueError("citations must be a list")
        if not isinstance(self.citation_required, bool):
            raise ValueError("citation_required must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class GeneratedSource:
    """Answer source list item compatible with citation verification."""

    source_id: str
    context_id: str
    document_id: str
    chunk_id: str
    title: str
    source_url: str
    space_key: str
    page_id: str = ""
    attachment_filename: str | None = None
    score: float = 0.0
    rerank_score: float = 0.0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for field_name in (
            "source_id",
            "context_id",
            "document_id",
            "chunk_id",
            "title",
            "source_url",
            "space_key",
        ):
            if not getattr(self, field_name):
                raise ValueError(f"{field_name} is required")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class StreamChunk:
    """후속 SSE adapter가 사용할 stream chunk schema."""

    generation_id: str
    chunk_index: int
    chunk_type: StreamChunkType | str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.chunk_type = _stream_chunk_type(self.chunk_type)
        self.validate()

    def validate(self) -> None:
        if not self.generation_id:
            raise ValueError("generation_id is required")
        if self.chunk_index < 0:
            raise ValueError("chunk_index must be greater than or equal to 0")
        if not self.chunk_type:
            raise ValueError("chunk_type is required")
        if not isinstance(self.content, str):
            raise ValueError("content must be a string")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be an object")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "generation_id": self.generation_id,
            "chunk_index": self.chunk_index,
            "chunk_type": _enum_or_str(self.chunk_type),
            "content": self.content,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class StreamingOutput:
    """MVP에서는 실제 SSE 전송 없이 streaming metadata만 표현한다."""

    streaming_supported: bool = False
    stream_chunks: list[StreamChunk] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.stream_chunks = [
            chunk if isinstance(chunk, StreamChunk) else StreamChunk(**chunk)
            for chunk in self.stream_chunks
        ]
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.streaming_supported, bool):
            raise ValueError("streaming_supported must be a boolean")
        for chunk in self.stream_chunks:
            chunk.validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "streaming_supported": self.streaming_supported,
            "stream_chunks": [chunk.to_dict() for chunk in self.stream_chunks],
        }


@dataclass(slots=True)
class AnswerOutput:
    """Answer Verification Agent가 소비할 canonical answer output."""

    generation_id: str
    conversation_id: str
    user_id: str
    answer_status: AnswerStatus | str
    answer: str
    sentences: list[GeneratedSentence]
    sources: list[GeneratedSource]
    used_context_ids: list[str]
    routing: dict[str, Any]
    model: str
    confidence: float
    insufficient_context: bool
    unsupported_gaps: list[str] = field(default_factory=list)
    streaming: StreamingOutput = field(default_factory=StreamingOutput)
    warnings: list[WarningItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.answer_status = _answer_status(self.answer_status)
        self.sentences = [
            sentence
            if isinstance(sentence, GeneratedSentence)
            else GeneratedSentence(**sentence)
            for sentence in self.sentences
        ]
        self.sources = [
            source if isinstance(source, GeneratedSource) else GeneratedSource(**source)
            for source in self.sources
        ]
        if not isinstance(self.streaming, StreamingOutput):
            self.streaming = StreamingOutput(**self.streaming)
        self.warnings = [
            warning
            if isinstance(warning, WarningItem)
            else _warning_item_from_dict(warning)
            for warning in self.warnings
        ]
        self.validate()

    def validate(self) -> None:
        if not self.generation_id:
            raise ValueError("generation_id is required")
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        if not self.user_id:
            raise ValueError("user_id is required")
        if not self.answer_status:
            raise ValueError("answer_status is required")
        if not self.answer:
            raise ValueError("answer is required")
        if not isinstance(self.used_context_ids, list):
            raise ValueError("used_context_ids must be a list")
        if not isinstance(self.routing, dict):
            raise ValueError("routing must be an object")
        if not self.model:
            raise ValueError("model is required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if not isinstance(self.insufficient_context, bool):
            raise ValueError("insufficient_context must be a boolean")
        if not isinstance(self.unsupported_gaps, list):
            raise ValueError("unsupported_gaps must be a list")
        for sentence in self.sentences:
            sentence.validate()
        for source in self.sources:
            source.validate()
        self.streaming.validate()
        for warning in self.warnings:
            warning.validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "generation_id": self.generation_id,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "answer_status": _enum_or_str(self.answer_status),
            "answer": self.answer,
            "sentences": [sentence.to_dict() for sentence in self.sentences],
            "sources": [source.to_dict() for source in self.sources],
            "used_context_ids": list(self.used_context_ids),
            "routing": dict(self.routing),
            "model": self.model,
            "confidence": self.confidence,
            "insufficient_context": self.insufficient_context,
            "unsupported_gaps": list(self.unsupported_gaps),
            "streaming": self.streaming.to_dict(),
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


@dataclass(slots=True)
class GenerationReport:
    """Answer generation job report schema."""

    job_id: str
    generation_id: str
    conversation_id: str
    status: GenerationReportStatus
    answer_status: AnswerStatus | str
    context_count: int
    used_context_count: int
    sentence_count: int
    citation_count: int
    warnings_count: int
    created_at: str

    def __post_init__(self) -> None:
        self.status = GenerationReportStatus(self.status)
        self.answer_status = _answer_status(self.answer_status)
        self.validate()

    def validate(self) -> None:
        if not self.job_id:
            raise ValueError("job_id is required")
        if not self.generation_id:
            raise ValueError("generation_id is required")
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        for field_name in (
            "context_count",
            "used_context_count",
            "sentence_count",
            "citation_count",
            "warnings_count",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be greater than or equal to 0")
        if not self.created_at:
            raise ValueError("created_at is required")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "job_id": self.job_id,
            "generation_id": self.generation_id,
            "conversation_id": self.conversation_id,
            "status": self.status.value,
            "answer_status": _enum_or_str(self.answer_status),
            "context_count": self.context_count,
            "used_context_count": self.used_context_count,
            "sentence_count": self.sentence_count,
            "citation_count": self.citation_count,
            "warnings_count": self.warnings_count,
            "created_at": self.created_at,
        }


def _task_prompt_type(value: TaskPromptType | str) -> TaskPromptType | str:
    if isinstance(value, TaskPromptType):
        return value
    return TaskPromptType.from_value(str(value))


def _answer_status(value: AnswerStatus | str) -> AnswerStatus | str:
    if isinstance(value, AnswerStatus):
        return value
    return AnswerStatus.from_value(str(value))


def _stream_chunk_type(value: StreamChunkType | str) -> StreamChunkType | str:
    if isinstance(value, StreamChunkType):
        return value
    try:
        return StreamChunkType(str(value))
    except ValueError:
        if not value:
            raise ValueError("chunk_type is required") from None
        return str(value)


def _warning_item_from_dict(payload: dict[str, Any]) -> WarningItem:
    return WarningItem(
        code=str(payload.get("code") or ""),
        message=str(payload.get("message") or ""),
    )


def _enum_or_str(value: Any) -> str:
    return value.value if isinstance(value, StrEnum) else str(value)
