from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : History Manager output을 Query Routing input schema로 로드/정규화.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature2 routing input normalization 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 json/dataclasses/pathlib 기반
--------------------------------------------------
"""

import json
from dataclasses import dataclass, field
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from query_routing_agent.schemas import (
    AclFilter,
    HistoryDecisionLabel,
    PreservedContext,
    QueryRoutingInput,
    WarningItem,
)

SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "openai_api_key",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "credential",
    "password",
}
RAW_HISTORY_KEYS = {"history", "raw_history", "conversation_history", "turns"}


class RoutingInputLoadError(ValueError):
    """Input JSON 파일을 로드할 수 없을 때 사용하는 safe error."""


class RoutingInputValidationError(ValueError):
    """Routing input validation 실패를 표현하는 safe error."""


@dataclass(slots=True)
class NormalizedRoutingInputResult:
    """Routing input normalization 결과."""

    routing_input: QueryRoutingInput
    acl_filter: AclFilter
    warnings: list[WarningItem] = field(default_factory=list)

    def to_safe_dict(self) -> dict[str, Any]:
        """로그/report에 사용할 수 있는 safe primitive dictionary를 반환한다."""
        return {
            "routing_input": self.routing_input.to_dict(),
            "acl_filter": self.acl_filter.to_dict(),
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


def load_history_manager_output(path: str | Path) -> dict[str, Any]:
    """History Manager output JSON file을 로드한다."""
    input_path = Path(path)
    try:
        raw_text = input_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RoutingInputLoadError(f"input JSON cannot be read: {input_path}") from exc

    try:
        payload = json.loads(raw_text)
    except JSONDecodeError as exc:
        raise RoutingInputLoadError("malformed JSON input") from exc

    if not isinstance(payload, dict):
        raise RoutingInputLoadError("input JSON must be an object")
    return payload


def load_and_normalize_routing_input(
    path: str | Path,
) -> NormalizedRoutingInputResult:
    """History Manager output JSON file을 로드하고 routing input으로 정규화한다."""
    return normalize_routing_input(load_history_manager_output(path))


def normalize_routing_input(
    payload: dict[str, Any],
) -> NormalizedRoutingInputResult:
    """History Manager output payload를 QueryRoutingInput으로 정규화한다."""
    if not isinstance(payload, dict):
        raise RoutingInputValidationError("routing input payload must be an object")

    warnings: list[WarningItem] = []
    working_payload = dict(payload)

    for raw_key in RAW_HISTORY_KEYS:
        if raw_key in working_payload:
            working_payload.pop(raw_key, None)
            warnings.append(
                WarningItem(
                    code="raw_history_dropped",
                    message="Raw history was omitted from normalized routing input.",
                )
            )

    conversation_id = _required_str(working_payload, "conversation_id")
    user_id = _required_str(working_payload, "user_id")
    original_question = _required_str(working_payload, "original_question")
    query = _required_str(working_payload, "query")

    history_decision = _normalize_history_decision(
        working_payload.get("history_decision"),
        warnings,
    )
    preserved_context = _normalize_preserved_context(
        working_payload.get("preserved_context"),
        warnings,
    )
    metadata = _normalize_metadata(working_payload.get("metadata"), warnings)
    acl_filter = AclFilter(user_id=user_id, groups=list(metadata["groups"]))

    try:
        routing_input = QueryRoutingInput(
            conversation_id=conversation_id,
            user_id=user_id,
            original_question=original_question,
            query=query,
            history_decision=history_decision,
            preserved_context=preserved_context,
            reset_required=bool(working_payload.get("reset_required", False)),
            metadata=metadata,
        )
    except ValueError as exc:
        raise RoutingInputValidationError(str(exc)) from exc

    return NormalizedRoutingInputResult(
        routing_input=routing_input,
        acl_filter=acl_filter,
        warnings=warnings,
    )


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RoutingInputValidationError(f"{key} is required")
    return value.strip()


def _normalize_history_decision(
    value: Any,
    warnings: list[WarningItem],
) -> HistoryDecisionLabel:
    if isinstance(value, HistoryDecisionLabel):
        return value
    if isinstance(value, str):
        try:
            return HistoryDecisionLabel(value)
        except ValueError:
            warnings.append(
                WarningItem(
                    code="unsupported_history_decision",
                    message="Unsupported history decision was replaced with ambiguous.",
                )
            )
            return HistoryDecisionLabel.AMBIGUOUS
    raise RoutingInputValidationError("history_decision is required")


def _normalize_preserved_context(
    value: Any,
    warnings: list[WarningItem],
) -> PreservedContext:
    if value is None:
        return PreservedContext()
    if not isinstance(value, dict):
        warnings.append(
            WarningItem(
                code="preserved_context_dropped",
                message="Malformed preserved context was replaced with defaults.",
            )
        )
        return PreservedContext()

    summary_value = value.get("summary", "")
    summary = "" if summary_value is None else str(summary_value)
    entities = _normalize_string_list(
        value.get("entities", []),
        "preserved_context_entities_normalized",
        "preserved_context_entities_dropped",
        warnings,
    )
    turn_refs = _normalize_string_list(
        value.get("turn_refs", []),
        "preserved_context_turn_refs_normalized",
        "preserved_context_turn_refs_dropped",
        warnings,
    )
    return PreservedContext(summary=summary, entities=entities, turn_refs=turn_refs)


def _normalize_metadata(
    value: Any,
    warnings: list[WarningItem],
) -> dict[str, Any]:
    if value is None:
        metadata: dict[str, Any] = {}
    elif isinstance(value, dict):
        metadata = dict(value)
    else:
        metadata = {}
        warnings.append(
            WarningItem(
                code="metadata_dropped",
                message="Malformed metadata was replaced with defaults.",
            )
        )

    normalized: dict[str, Any] = {}
    for key, item in metadata.items():
        normalized_key = str(key)
        key_lower = normalized_key.lower()
        if key_lower in SENSITIVE_KEYS:
            warnings.append(
                WarningItem(
                    code="sensitive_metadata_dropped",
                    message="Sensitive metadata field was omitted.",
                )
            )
            continue
        if key_lower in RAW_HISTORY_KEYS:
            warnings.append(
                WarningItem(
                    code="metadata_raw_history_dropped",
                    message="Raw history metadata was omitted.",
                )
            )
            continue
        normalized[normalized_key] = item

    normalized["groups"] = _normalize_string_list(
        normalized.get("groups", []),
        "metadata_groups_normalized",
        "metadata_groups_dropped",
        warnings,
    )
    normalized["space_keys"] = _normalize_string_list(
        normalized.get("space_keys", []),
        "metadata_space_keys_normalized",
        "metadata_space_keys_dropped",
        warnings,
    )
    return normalized


def _normalize_string_list(
    value: Any,
    normalized_code: str,
    dropped_code: str,
    warnings: list[WarningItem],
) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            warnings.append(
                WarningItem(
                    code=normalized_code,
                    message="String value was converted to a single-item list.",
                )
            )
            return [stripped]
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    warnings.append(
        WarningItem(
            code=dropped_code,
            message="Non-list value was dropped during normalization.",
        )
    )
    return []
