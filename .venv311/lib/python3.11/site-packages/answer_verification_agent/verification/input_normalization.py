from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Verification Agent verification input 로드/정규화 서비스.
          feature2 범위에서는 sentence/citation parsing 없이 후속 단계가 사용할
          Answer Generation output과 context의 canonical shape만 준비한다.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature2 input normalization 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/json/pathlib 기반
--------------------------------------------------
"""

import json
import re
from dataclasses import dataclass, field
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from answer_verification_agent.schemas import VerificationInput, WarningItem
from answer_verification_agent.schemas._serialization import to_primitive

_ANSWER_OUTPUT_FIELDS = {
    "generation_id",
    "answer_status",
    "answer",
    "sentences",
    "sources",
    "used_context_ids",
    "routing",
    "model",
    "confidence",
    "warnings",
}
_CONTEXT_FIELDS = {
    "context_id",
    "document_id",
    "chunk_id",
    "title",
    "space_key",
    "source_url",
    "content",
    "score",
    "rerank_score",
    "metadata",
}
_SENSITIVE_PATTERNS = [
    re.compile(r"OPENAI_API_KEY\s*=\s*[^,\s]+", re.IGNORECASE),
    re.compile(r"Authorization:\s*Bearer\s+[^,\s]+", re.IGNORECASE),
    re.compile(r"Bearer\s+[^,\s]+", re.IGNORECASE),
]


class VerificationInputNormalizationError(Exception):
    """Safe non-secret normalization error."""

    def __init__(self, message: str, *, error_type: str, retryable: bool) -> None:
        super().__init__(_redact_text(message))
        self.error_type = error_type
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        """Report/log safe error payload."""
        return {
            "error_type": self.error_type,
            "retryable": self.retryable,
            "message": str(self),
        }


@dataclass(slots=True)
class NormalizedAnswerOutput:
    """Answer Generation output의 feature2 canonical subset."""

    generation_id: str
    answer_status: str
    answer: str
    sentences: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    used_context_ids: list[str]
    routing: dict[str, Any]
    model: str
    confidence: float
    warnings: list[dict[str, Any]]
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _redact_payload(to_primitive(self))


@dataclass(slots=True)
class NormalizedContext:
    """Top context canonical subset."""

    context_id: str
    document_id: str
    chunk_id: str
    title: str
    space_key: str
    source_url: str
    content: str
    score: float
    rerank_score: float
    metadata: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _redact_payload(to_primitive(self))


@dataclass(slots=True)
class NormalizedVerificationInput:
    """Normalized verification input result."""

    verification_input: VerificationInput
    answer_output: NormalizedAnswerOutput
    contexts: list[NormalizedContext]
    metadata: dict[str, Any]
    warnings: list[WarningItem] = field(default_factory=list)
    requires_sentence_fallback: bool = False
    has_contexts: bool = True
    low_confidence_ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _redact_payload(
            {
                "verification_input": self.verification_input.to_dict(),
                "answer_output": self.answer_output.to_dict(),
                "contexts": [context.to_dict() for context in self.contexts],
                "metadata": self.metadata,
                "warnings": [warning.to_dict() for warning in self.warnings],
                "requires_sentence_fallback": self.requires_sentence_fallback,
                "has_contexts": self.has_contexts,
                "low_confidence_ready": self.low_confidence_ready,
            }
        )


def load_verification_input(path: Path | str) -> NormalizedVerificationInput:
    """JSON 파일을 로드하고 verification input을 정규화한다."""
    input_path = Path(path)
    try:
        with input_path.open("r", encoding="utf-8") as input_file:
            payload = json.load(input_file)
    except JSONDecodeError as exc:
        raise VerificationInputNormalizationError(
            f"malformed JSON input: {exc.msg}",
            error_type="malformed_json",
            retryable=False,
        ) from exc
    except OSError as exc:
        raise VerificationInputNormalizationError(
            f"input JSON cannot be read: {exc}",
            error_type="input_read_error",
            retryable=False,
        ) from exc
    return normalize_verification_input(payload)


def normalize_verification_input(
    payload: dict[str, Any],
) -> NormalizedVerificationInput:
    """Answer Generation output + Top contexts를 feature2 canonical shape로 정규화한다."""
    if not isinstance(payload, dict):
        raise VerificationInputNormalizationError(
            "verification input must be a JSON object",
            error_type="validation_error",
            retryable=False,
        )
    try:
        verification_input = VerificationInput.from_dict(payload)
    except ValueError as exc:
        raise VerificationInputNormalizationError(
            str(exc),
            error_type="validation_error",
            retryable=False,
        ) from exc

    warnings: list[WarningItem] = []
    answer_output = _normalize_answer_output(verification_input.answer_output)
    if not answer_output.sentences:
        warnings.append(
            WarningItem(
                code="sentence_fallback_required",
                message="Answer output has no generated sentences; sentence parser fallback is required.",
            )
        )

    contexts = _normalize_contexts(verification_input.contexts, warnings)
    if not contexts:
        warnings.append(
            WarningItem(
                code="contexts_empty",
                message="No usable verification contexts were provided; low-confidence verification should be prepared.",
            )
        )

    has_contexts = bool(contexts)
    return NormalizedVerificationInput(
        verification_input=verification_input,
        answer_output=answer_output,
        contexts=contexts,
        metadata=_redact_payload(dict(verification_input.metadata)),
        warnings=warnings,
        requires_sentence_fallback=not answer_output.sentences,
        has_contexts=has_contexts,
        low_confidence_ready=not has_contexts,
    )


def _normalize_answer_output(payload: dict[str, Any]) -> NormalizedAnswerOutput:
    extra = {
        key: value
        for key, value in payload.items()
        if key not in _ANSWER_OUTPUT_FIELDS
    }
    return NormalizedAnswerOutput(
        generation_id=str(payload.get("generation_id") or ""),
        answer_status=str(payload.get("answer_status") or ""),
        answer=str(payload.get("answer") or ""),
        sentences=_list_of_dicts(payload.get("sentences")),
        sources=_list_of_dicts(payload.get("sources")),
        used_context_ids=[str(item) for item in payload.get("used_context_ids") or []],
        routing=dict(payload.get("routing") or {}),
        model=str(payload.get("model") or ""),
        confidence=float(payload.get("confidence", 0.0)),
        warnings=_list_of_dicts(payload.get("warnings")),
        extra=_redact_payload(extra),
    )


def _normalize_contexts(
    payloads: list[dict[str, Any]],
    warnings: list[WarningItem],
) -> list[NormalizedContext]:
    contexts: list[NormalizedContext] = []
    seen_context_ids: set[str] = set()
    for index, payload in enumerate(payloads):
        if not isinstance(payload, dict):
            warnings.append(
                WarningItem(
                    code="context_invalid",
                    message=f"Context at index {index} is not an object and was skipped.",
                )
            )
            continue
        context_id = str(payload.get("context_id") or "")
        if not context_id:
            warnings.append(
                WarningItem(
                    code="context_id_missing",
                    message=f"Context at index {index} has no context_id and was skipped.",
                )
            )
            continue
        if context_id in seen_context_ids:
            warnings.append(
                WarningItem(
                    code="context_duplicate",
                    message=f"Duplicate context_id {context_id} was skipped.",
                )
            )
            continue
        seen_context_ids.add(context_id)
        extra = {
            key: value
            for key, value in payload.items()
            if key not in _CONTEXT_FIELDS
        }
        contexts.append(
            NormalizedContext(
                context_id=context_id,
                document_id=str(payload.get("document_id") or ""),
                chunk_id=str(payload.get("chunk_id") or ""),
                title=str(payload.get("title") or ""),
                space_key=str(payload.get("space_key") or ""),
                source_url=str(payload.get("source_url") or ""),
                content=str(payload.get("content") or ""),
                score=float(payload.get("score", 0.0)),
                rerank_score=float(payload.get("rerank_score", 0.0)),
                metadata=_redact_payload(dict(payload.get("metadata") or {})),
                extra=_redact_payload(extra),
            )
        )
    return contexts[:5]


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        _redact_payload(dict(item))
        for item in value
        if isinstance(item, dict)
    ]


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
