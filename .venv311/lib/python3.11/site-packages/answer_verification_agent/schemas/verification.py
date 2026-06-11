from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Verification Agent feature1 canonical schema 정의.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, verification input/output/report schema 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/enum 기반
--------------------------------------------------
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from answer_verification_agent.schemas._serialization import to_primitive


class VerificationOverallLabel(StrEnum):
    """Overall verification label."""

    PASS = "PASS"
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"


class SentenceLabel(StrEnum):
    """Sentence-level verification label."""

    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    NOT_CHECKED = "NOT_CHECKED"


class QCAQualityLabel(StrEnum):
    """QCA local output quality label."""

    ACCEPTED = "accepted"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class VerificationReportStatus(StrEnum):
    """Verification report status."""

    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


@dataclass(slots=True)
class WarningItem:
    """Verification process warning schema."""

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
class VerificationInput:
    """Answer Generation output과 Top context를 포함하는 verification input."""

    conversation_id: str
    user_id: str
    answer_output: dict[str, Any]
    contexts: list[dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        if not self.user_id:
            raise ValueError("user_id is required")
        if not isinstance(self.answer_output, dict) or not self.answer_output:
            raise ValueError("answer_output is required")
        if not isinstance(self.contexts, list):
            raise ValueError("contexts must be a list")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be an object")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "VerificationInput":
        return cls(
            conversation_id=str(payload.get("conversation_id") or ""),
            user_id=str(payload.get("user_id") or ""),
            answer_output=payload.get("answer_output") or {},
            contexts=payload.get("contexts") or [],
            metadata=payload.get("metadata") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class CitationCoverage:
    """Citation coverage summary schema."""

    total_sentences: int
    sentences_with_citations: int
    valid_citations: int
    invalid_citations: int
    coverage_ratio: float

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.total_sentences < 0:
            raise ValueError("total_sentences must be greater than or equal to 0")
        if self.sentences_with_citations < 0:
            raise ValueError(
                "sentences_with_citations must be greater than or equal to 0"
            )
        if self.valid_citations < 0:
            raise ValueError("valid_citations must be greater than or equal to 0")
        if self.invalid_citations < 0:
            raise ValueError("invalid_citations must be greater than or equal to 0")
        if not 0 <= self.coverage_ratio <= 1:
            raise ValueError("coverage_ratio must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class SentenceVerificationResult:
    """Sentence-level verification result schema."""

    sentence_id: str
    text: str
    label: SentenceLabel | str
    score: float
    citations: list[str]
    matched_context_ids: list[str]
    failed_rules: list[str]
    llm_evaluation_used: bool
    reason: str

    def __post_init__(self) -> None:
        self.label = _sentence_label(self.label)
        self.validate()

    def validate(self) -> None:
        if not self.sentence_id:
            raise ValueError("sentence_id is required")
        if not self.text:
            raise ValueError("text is required")
        if not self.label:
            raise ValueError("label is required")
        if not 0 <= self.score <= 1:
            raise ValueError("score must be between 0 and 1")
        if not isinstance(self.citations, list):
            raise ValueError("citations must be a list")
        if not isinstance(self.matched_context_ids, list):
            raise ValueError("matched_context_ids must be a list")
        if not isinstance(self.failed_rules, list):
            raise ValueError("failed_rules must be a list")
        if not isinstance(self.llm_evaluation_used, bool):
            raise ValueError("llm_evaluation_used must be a boolean")
        if not self.reason:
            raise ValueError("reason is required")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class UIWarning:
    """UI warning metadata schema. UI 렌더링 자체는 MVP 범위가 아니다."""

    warning_level: str
    warning_reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.warning_level not in {"none", "low", "medium", "high"}:
            raise ValueError("warning_level must be one of none, low, medium, high")
        if not isinstance(self.warning_reasons, list):
            raise ValueError("warning_reasons must be a list")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class RegenerationRequest:
    """Answer Generation Agent 재호출 없이 생성하는 regeneration request payload."""

    target_generation_id: str
    unsupported_sentence_ids: list[str]
    guidance: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.target_generation_id:
            raise ValueError("target_generation_id is required")
        if not isinstance(self.unsupported_sentence_ids, list):
            raise ValueError("unsupported_sentence_ids must be a list")
        if not self.guidance:
            raise ValueError("guidance is required")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class VerificationOutput:
    """Canonical verification output schema."""

    verification_id: str
    generation_id: str
    conversation_id: str
    user_id: str
    overall_label: VerificationOverallLabel | str
    overall_score: float
    sentence_results: list[SentenceVerificationResult]
    unsupported_claims: list[dict[str, Any]]
    citation_coverage: CitationCoverage
    llm_evaluation_used: bool
    ui_warning_required: bool
    ui_warning: UIWarning
    qca_candidate: bool
    qca_output_ref: str | None
    regeneration_recommended: bool
    regeneration_request: RegenerationRequest | None
    warnings: list[WarningItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.overall_label = _overall_label(self.overall_label)
        self.sentence_results = [
            item
            if isinstance(item, SentenceVerificationResult)
            else _sentence_result_from_dict(item)
            for item in self.sentence_results
        ]
        self.warnings = [
            warning
            if isinstance(warning, WarningItem)
            else _warning_item_from_dict(warning)
            for warning in self.warnings
        ]
        self.validate()

    def validate(self) -> None:
        if not self.verification_id:
            raise ValueError("verification_id is required")
        if not self.generation_id:
            raise ValueError("generation_id is required")
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        if not self.user_id:
            raise ValueError("user_id is required")
        if not self.overall_label:
            raise ValueError("overall_label is required")
        if not 0 <= self.overall_score <= 1:
            raise ValueError("overall_score must be between 0 and 1")
        if not isinstance(self.unsupported_claims, list):
            raise ValueError("unsupported_claims must be a list")
        if not isinstance(self.llm_evaluation_used, bool):
            raise ValueError("llm_evaluation_used must be a boolean")
        if not isinstance(self.ui_warning_required, bool):
            raise ValueError("ui_warning_required must be a boolean")
        if not isinstance(self.qca_candidate, bool):
            raise ValueError("qca_candidate must be a boolean")
        if not isinstance(self.regeneration_recommended, bool):
            raise ValueError("regeneration_recommended must be a boolean")
        for sentence in self.sentence_results:
            sentence.validate()
        self.citation_coverage.validate()
        self.ui_warning.validate()
        if self.regeneration_request is not None:
            self.regeneration_request.validate()
        for warning in self.warnings:
            warning.validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class QCAOutput:
    """Local QCA output schema. DB 저장은 MVP 제외다."""

    qca_id: str
    conversation_id: str
    generation_id: str
    verification_id: str
    question: str
    context_refs: list[str]
    answer: str
    overall_label: VerificationOverallLabel | str
    overall_score: float
    quality_label: QCAQualityLabel | str
    created_at: str

    def __post_init__(self) -> None:
        self.overall_label = _overall_label(self.overall_label)
        self.quality_label = _qca_quality_label(self.quality_label)
        self.validate()

    def validate(self) -> None:
        if not self.qca_id:
            raise ValueError("qca_id is required")
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        if not self.generation_id:
            raise ValueError("generation_id is required")
        if not self.verification_id:
            raise ValueError("verification_id is required")
        if not self.question:
            raise ValueError("question is required")
        if not isinstance(self.context_refs, list):
            raise ValueError("context_refs must be a list")
        if not self.answer:
            raise ValueError("answer is required")
        if not 0 <= self.overall_score <= 1:
            raise ValueError("overall_score must be between 0 and 1")
        if not self.created_at:
            raise ValueError("created_at is required")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return to_primitive(self)


@dataclass(slots=True)
class VerificationReport:
    """Verification job report schema."""

    job_id: str
    verification_id: str
    generation_id: str
    conversation_id: str
    status: VerificationReportStatus | str
    overall_label: VerificationOverallLabel | str
    sentence_count: int
    unsupported_count: int
    low_confidence_count: int
    llm_evaluation_count: int
    warnings_count: int
    created_at: str

    def __post_init__(self) -> None:
        self.status = _report_status(self.status)
        self.overall_label = _overall_label(self.overall_label)
        self.validate()

    def validate(self) -> None:
        if not self.job_id:
            raise ValueError("job_id is required")
        if not self.verification_id:
            raise ValueError("verification_id is required")
        if not self.generation_id:
            raise ValueError("generation_id is required")
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        for field_name in (
            "sentence_count",
            "unsupported_count",
            "low_confidence_count",
            "llm_evaluation_count",
            "warnings_count",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be greater than or equal to 0")
        if not self.created_at:
            raise ValueError("created_at is required")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return to_primitive(self)


def _sentence_label(value: SentenceLabel | str) -> SentenceLabel | str:
    try:
        return SentenceLabel(value)
    except ValueError:
        if not value:
            raise ValueError("label is required") from None
        return value


def _overall_label(
    value: VerificationOverallLabel | str,
) -> VerificationOverallLabel | str:
    try:
        return VerificationOverallLabel(value)
    except ValueError:
        if not value:
            raise ValueError("overall_label is required") from None
        return value


def _qca_quality_label(value: QCAQualityLabel | str) -> QCAQualityLabel | str:
    try:
        return QCAQualityLabel(value)
    except ValueError:
        if not value:
            raise ValueError("quality_label is required") from None
        return value


def _report_status(
    value: VerificationReportStatus | str,
) -> VerificationReportStatus | str:
    try:
        return VerificationReportStatus(value)
    except ValueError:
        if not value:
            raise ValueError("status is required") from None
        return value


def _sentence_result_from_dict(
    payload: dict[str, Any],
) -> SentenceVerificationResult:
    return SentenceVerificationResult(
        sentence_id=str(payload.get("sentence_id") or ""),
        text=str(payload.get("text") or ""),
        label=str(payload.get("label") or ""),
        score=float(payload.get("score", 0.0)),
        citations=payload.get("citations") or [],
        matched_context_ids=payload.get("matched_context_ids") or [],
        failed_rules=payload.get("failed_rules") or [],
        llm_evaluation_used=bool(payload.get("llm_evaluation_used", False)),
        reason=str(payload.get("reason") or ""),
    )


def _warning_item_from_dict(payload: dict[str, Any]) -> WarningItem:
    return WarningItem(
        code=str(payload.get("code") or ""),
        message=str(payload.get("message") or ""),
    )
