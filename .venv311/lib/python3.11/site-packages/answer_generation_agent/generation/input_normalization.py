from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Generation Agent generation input JSON 로드 및 정규화.
          Query Routing output과 Top context 입력을 안전한 내부 schema로 변환한다.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from answer_generation_agent.schemas import (
    GenerationInput,
    RoutingDecisionInput,
    SearchResults,
    TaskPromptType,
    TopContext,
    WarningItem,
)
from answer_generation_agent.schemas._serialization import to_primitive

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "openai_api_key",
    "secret",
    "token",
}


class GenerationInputLoadError(ValueError):
    """Generation input file을 로드할 수 없을 때 발생하는 safe error."""


class GenerationInputNormalizationError(ValueError):
    """Generation input을 내부 schema로 정규화할 수 없을 때 발생하는 safe error."""


@dataclass(slots=True)
class NormalizedGenerationInputResult:
    """Feature2 generation input normalization result."""

    generation_input: GenerationInput
    normalized_contexts: list[TopContext]
    warnings: list[WarningItem] = field(default_factory=list)
    input_context_count: int = 0
    used_context_count: int = 0
    insufficient_context_candidate: bool = False

    def __post_init__(self) -> None:
        self.warnings = [
            warning
            if isinstance(warning, WarningItem)
            else WarningItem(
                code=str(warning.get("code") or "normalization_warning"),
                message=str(warning.get("message") or "Input normalization warning."),
            )
            for warning in self.warnings
        ]
        self.used_context_count = len(self.normalized_contexts)
        self.insufficient_context_candidate = self.used_context_count == 0

    def to_dict(self) -> dict[str, Any]:
        """Safe serialization을 반환한다."""
        return {
            "generation_input": _sanitize_value(self.generation_input.to_dict()),
            "normalized_contexts": [
                _sanitize_value(context.to_dict()) for context in self.normalized_contexts
            ],
            "warnings": [warning.to_dict() for warning in self.warnings],
            "input_context_count": self.input_context_count,
            "used_context_count": self.used_context_count,
            "insufficient_context_candidate": self.insufficient_context_candidate,
        }


def load_generation_input_json(path: Path) -> dict[str, Any]:
    """Generation input JSON 파일을 object payload로 로드한다.

    Args:
        path: Generation input JSON 파일 경로.

    Returns:
        JSON object payload.

    Raises:
        GenerationInputLoadError: 파일이 malformed JSON이거나 object가 아닌 경우.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GenerationInputLoadError("malformed JSON in generation input") from exc
    if not isinstance(payload, dict):
        raise GenerationInputLoadError("generation input JSON must be an object")
    return payload


def normalize_generation_input(
    payload: dict[str, Any],
    *,
    max_contexts: int = 5,
) -> NormalizedGenerationInputResult:
    """Query Routing output + Top context payload를 내부 schema로 정규화한다.

    Args:
        payload: Generation input JSON object.
        max_contexts: 사용할 최대 context 개수. MVP 기본값은 5.

    Returns:
        NormalizedGenerationInputResult.

    Raises:
        GenerationInputNormalizationError: 필수 payload가 누락되어 job 수행이
            불가능한 경우.
    """
    if not isinstance(payload, dict):
        raise GenerationInputNormalizationError("generation input must be an object")
    if max_contexts <= 0:
        raise GenerationInputNormalizationError("max_contexts must be greater than 0")

    warnings: list[WarningItem] = []
    _require_key(payload, "conversation_id")
    _require_key(payload, "user_id")
    routing_payload = _require_object(payload, "routing_decision")
    search_results_payload = _require_object(payload, "search_results")

    routing_decision = _normalize_routing_decision(routing_payload, warnings)
    top_context_payloads = _normalize_top_context_payloads(search_results_payload)
    input_context_count = len(top_context_payloads)
    normalized_contexts = _normalize_top_contexts(
        top_context_payloads,
        max_contexts=max_contexts,
        warnings=warnings,
    )
    metadata = _sanitize_metadata(payload.get("metadata") or {})
    generation_input = GenerationInput(
        conversation_id=str(payload.get("conversation_id") or ""),
        user_id=str(payload.get("user_id") or ""),
        routing_decision=routing_decision,
        search_results=SearchResults(top_contexts=normalized_contexts),
        metadata=metadata,
    )

    return NormalizedGenerationInputResult(
        generation_input=generation_input,
        normalized_contexts=normalized_contexts,
        warnings=warnings,
        input_context_count=input_context_count,
    )


def _normalize_routing_decision(
    payload: dict[str, Any],
    warnings: list[WarningItem],
) -> RoutingDecisionInput:
    _require_key(payload, "routing_id")
    _require_key(payload, "query")
    _require_key(payload, "intent")
    _require_key(payload, "task_prompt_type")

    normalized_payload = dict(payload)
    task_prompt_type = str(payload.get("task_prompt_type") or "")
    if task_prompt_type not in {item.value for item in TaskPromptType}:
        normalized_payload["task_prompt_type"] = TaskPromptType.GENERAL.value
        warnings.append(
            WarningItem(
                code="unsupported_task_prompt_type",
                message="Unsupported task prompt type was replaced with general.",
            )
        )
    normalized_payload["metadata_filters"] = _object_or_empty(
        payload.get("metadata_filters")
    )
    normalized_payload["pool_weights"] = _object_or_empty(payload.get("pool_weights"))
    normalized_payload["expanded_queries"] = _string_list(
        payload.get("expanded_queries")
    )
    normalized_payload["warnings"] = _safe_warning_payloads(payload.get("warnings"))

    try:
        return RoutingDecisionInput.from_dict(normalized_payload)
    except (TypeError, ValueError) as exc:
        raise GenerationInputNormalizationError(_safe_message(str(exc))) from exc


def _normalize_top_context_payloads(
    search_results_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    top_contexts = search_results_payload.get("top_contexts", [])
    if top_contexts is None:
        return []
    if not isinstance(top_contexts, list):
        raise GenerationInputNormalizationError("top_contexts must be a list")
    return [context for context in top_contexts if isinstance(context, dict)]


def _normalize_top_contexts(
    top_context_payloads: list[dict[str, Any]],
    *,
    max_contexts: int,
    warnings: list[WarningItem],
) -> list[TopContext]:
    ranked_payloads = sorted(
        enumerate(top_context_payloads),
        key=lambda item: _context_sort_key(item[0], item[1]),
        reverse=True,
    )
    contexts: list[TopContext] = []
    seen_context_ids: set[str] = set()

    for _, context_payload in ranked_payloads:
        context_id = str(context_payload.get("context_id") or "")
        content = str(context_payload.get("content") or "")
        if not content.strip():
            warnings.append(
                WarningItem(
                    code="empty_context_content",
                    message="A top context with empty content was excluded.",
                )
            )
            continue
        if context_id in seen_context_ids:
            warnings.append(
                WarningItem(
                    code="duplicate_context_id",
                    message="A duplicate context id was excluded deterministically.",
                )
            )
            continue
        try:
            context = TopContext.from_dict(_sanitize_context_payload(context_payload))
        except (TypeError, ValueError) as exc:
            warnings.append(
                WarningItem(
                    code="invalid_context",
                    message=_safe_message(str(exc)),
                )
            )
            continue
        seen_context_ids.add(context.context_id)
        contexts.append(context)
        if len(contexts) >= max_contexts:
            break

    return contexts


def _context_sort_key(index: int, payload: dict[str, Any]) -> tuple[int, float, int]:
    if _has_number(payload, "rerank_score"):
        return (2, float(payload["rerank_score"]), -index)
    if _has_number(payload, "score"):
        return (1, float(payload["score"]), -index)
    return (0, 0.0, -index)


def _has_number(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if isinstance(value, bool):
        return False
    return isinstance(value, int | float)


def _sanitize_context_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    sanitized["content"] = str(payload.get("content") or "").strip()
    sanitized["metadata"] = _sanitize_metadata(payload.get("metadata") or {})
    return sanitized


def _require_key(payload: dict[str, Any], key: str) -> None:
    if key not in payload or payload.get(key) in (None, ""):
        raise GenerationInputNormalizationError(f"{key} is required")


def _require_object(payload: dict[str, Any], key: str) -> dict[str, Any]:
    _require_key(payload, key)
    value = payload.get(key)
    if not isinstance(value, dict):
        raise GenerationInputNormalizationError(f"{key} must be an object")
    return value


def _object_or_empty(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return _sanitize_metadata(value)
    return {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _safe_warning_payloads(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    warnings: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        code = _safe_message(str(item.get("code") or "routing_warning"))
        message = _safe_message(str(item.get("message") or "Routing warning."))
        warnings.append({"code": code, "message": message})
    return warnings


def _sanitize_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        if _is_sensitive_key(str(key)):
            continue
        sanitized[str(key)] = _sanitize_value(item)
    return sanitized


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _sanitize_metadata(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        return _safe_message(value)
    return to_primitive(value)


def _safe_message(message: str) -> str:
    redacted = message
    for marker in (
        "OPENAI_API_KEY",
        "Authorization",
        "api key",
        "API key",
        "token",
        "secret",
        "synthetic-marker",
    ):
        redacted = redacted.replace(marker, "<redacted>")
    return redacted


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(marker in normalized for marker in _SENSITIVE_KEYS)
