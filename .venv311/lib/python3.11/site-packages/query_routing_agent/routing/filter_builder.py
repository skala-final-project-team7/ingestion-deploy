from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Query Routing Agent feature5 metadata filter, ACL payload,
          task prompt type, Multi-Pool weight builder 구현.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature5 filter/weight builder 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses 기반
--------------------------------------------------
"""

from dataclasses import dataclass, field
from typing import Any

from query_routing_agent.config import QueryRoutingConfig
from query_routing_agent.schemas import (
    AclFilter,
    DateRangeFilter,
    IntentLabel,
    MetadataFilter,
    PoolWeights,
    TaskPromptType,
    WarningItem,
)

from .normalization import NormalizedRoutingInputResult

DEFAULT_POOL_WEIGHTS = {"title": 0.25, "content": 0.6, "label": 0.15}
INTENT_POOL_WEIGHTS = {
    IntentLabel.INCIDENT_RESPONSE: {"title": 0.2, "content": 0.65, "label": 0.15},
    IntentLabel.OPERATIONS_GUIDE: DEFAULT_POOL_WEIGHTS,
    IntentLabel.POLICY_PROCEDURE: {"title": 0.3, "content": 0.6, "label": 0.1},
    IntentLabel.HISTORY_LOOKUP: {"title": 0.2, "content": 0.5, "label": 0.3},
    IntentLabel.UNKNOWN: DEFAULT_POOL_WEIGHTS,
}
TASK_PROMPT_TYPES = {
    IntentLabel.INCIDENT_RESPONSE: TaskPromptType.TIMELINE,
    IntentLabel.OPERATIONS_GUIDE: TaskPromptType.STEP_BY_STEP,
    IntentLabel.POLICY_PROCEDURE: TaskPromptType.EVIDENCE_FIRST,
    IntentLabel.HISTORY_LOOKUP: TaskPromptType.HISTORY_SUMMARY,
    IntentLabel.UNKNOWN: TaskPromptType.GENERAL,
}
SENSITIVE_MARKERS = (
    "OPENAI_API_KEY",
    "Authorization",
    "Bearer",
    "api_key",
    "api key",
    "access_token",
    "token",
    "secret",
)


@dataclass(slots=True)
class FilterAndWeightResult:
    """Filter/weight builder result."""

    metadata_filter: MetadataFilter
    task_prompt_type: TaskPromptType
    pool_weights: PoolWeights
    warnings: list[WarningItem] = field(default_factory=list)

    def to_safe_dict(self) -> dict[str, Any]:
        """로그/report에 사용할 수 있는 primitive dictionary를 반환한다."""
        return {
            "metadata_filter": self.metadata_filter.to_dict(),
            "task_prompt_type": self.task_prompt_type.value,
            "pool_weights": self.pool_weights.to_dict(),
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


def build_filter_and_pool_weights(
    normalized_input: NormalizedRoutingInputResult,
    intent: IntentLabel | str,
    config: QueryRoutingConfig,
) -> FilterAndWeightResult:
    """Routing metadata와 intent를 filter, task prompt, pool weight로 변환한다."""
    warnings: list[WarningItem] = []
    metadata_filter = build_metadata_filter(normalized_input, warnings)
    task_prompt_type = map_task_prompt_type(intent)
    pool_weights, weight_warnings = build_pool_weights(intent, config)
    warnings.extend(weight_warnings)
    return FilterAndWeightResult(
        metadata_filter=metadata_filter,
        task_prompt_type=task_prompt_type,
        pool_weights=pool_weights,
        warnings=warnings,
    )


def build_metadata_filter(
    normalized_input: NormalizedRoutingInputResult,
    warnings: list[WarningItem] | None = None,
) -> MetadataFilter:
    """Routing input metadata를 canonical metadata filter로 변환한다."""
    collected_warnings = warnings if warnings is not None else []
    metadata = normalized_input.routing_input.metadata
    groups = _safe_string_list(metadata.get("groups", []), "acl_groups", collected_warnings)
    if not groups:
        collected_warnings.append(
            WarningItem(
                code="acl_groups_missing",
                message="ACL groups were missing; only user_id was forwarded.",
            )
        )
    return MetadataFilter(
        space_keys=_safe_string_list(
            metadata.get("space_keys", []),
            "metadata_space_keys",
            collected_warnings,
        ),
        labels=_safe_string_list(
            metadata.get("labels", []),
            "metadata_labels",
            collected_warnings,
        ),
        document_types=_safe_string_list(
            metadata.get("document_types", []),
            "metadata_document_types",
            collected_warnings,
        ),
        source_types=_safe_string_list(
            metadata.get("source_types", []),
            "metadata_source_types",
            collected_warnings,
        ),
        date_range=_safe_date_range(metadata.get("date_range"), collected_warnings),
        attachment_required=_safe_bool(
            metadata.get("attachment_required", False),
            "metadata_attachment_required",
            collected_warnings,
        ),
        acl=AclFilter(
            user_id=normalized_input.routing_input.user_id,
            groups=groups,
        ),
    )


def map_task_prompt_type(intent: IntentLabel | str) -> TaskPromptType:
    """Intent를 Answer Generation용 task prompt type으로 매핑한다."""
    normalized_intent = _intent_or_unknown(intent)
    return TASK_PROMPT_TYPES.get(normalized_intent, TaskPromptType.GENERAL)


def build_pool_weights(
    intent: IntentLabel | str,
    config: QueryRoutingConfig,
) -> tuple[PoolWeights, list[WarningItem]]:
    """Intent별 Multi-Pool weight를 생성한다."""
    normalized_intent = _intent_or_unknown(intent)
    raw_weights = INTENT_POOL_WEIGHTS.get(normalized_intent)
    if raw_weights is None:
        raw_weights = config.default_pool_weights.to_dict()
    return normalize_pool_weights(raw_weights)


def normalize_pool_weights(raw_weights: dict[str, Any]) -> tuple[PoolWeights, list[WarningItem]]:
    """Raw weight를 검증하고 합계 1.0으로 normalize한다."""
    warnings: list[WarningItem] = []
    try:
        title = float(raw_weights["title"])
        content = float(raw_weights["content"])
        label = float(raw_weights["label"])
    except (KeyError, TypeError, ValueError):
        return _default_pool_weights_with_warning()

    if title < 0 or content < 0 or label < 0:
        return _default_pool_weights_with_warning()
    total = title + content + label
    if total <= 0:
        return _default_pool_weights_with_warning()

    normalized = {
        "title": title / total,
        "content": content / total,
        "label": label / total,
    }
    if abs(total - 1.0) > 0.000001:
        warnings.append(
            WarningItem(
                code="pool_weights_normalized",
                message="Pool weights were normalized to sum to 1.0.",
            )
        )
    return PoolWeights(
        title=round(normalized["title"], 10),
        content=round(normalized["content"], 10),
        label=round(normalized["label"], 10),
    ), warnings


def _default_pool_weights_with_warning() -> tuple[PoolWeights, list[WarningItem]]:
    return PoolWeights(), [
        WarningItem(
            code="pool_weights_defaulted",
            message="Invalid pool weights were replaced with defaults.",
        )
    ]


def _intent_or_unknown(intent: IntentLabel | str) -> IntentLabel:
    if isinstance(intent, IntentLabel):
        return intent
    try:
        return IntentLabel(str(intent))
    except ValueError:
        return IntentLabel.UNKNOWN


def _safe_string_list(
    value: Any,
    field_name: str,
    warnings: list[WarningItem],
) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = _safe_string(value)
        return [cleaned] if cleaned else []
    if not isinstance(value, list):
        warnings.append(
            WarningItem(
                code=f"{field_name}_dropped",
                message="Invalid metadata filter value was dropped.",
            )
        )
        return []

    items = []
    for item in value:
        cleaned = _safe_string(str(item))
        if cleaned:
            items.append(cleaned)
    return _dedupe(items)


def _safe_date_range(value: Any, warnings: list[WarningItem]) -> DateRangeFilter:
    if value is None:
        return DateRangeFilter()
    if not isinstance(value, dict):
        warnings.append(
            WarningItem(
                code="metadata_date_range_dropped",
                message="Invalid date_range metadata was dropped.",
            )
        )
        return DateRangeFilter()
    return DateRangeFilter(
        from_date=_safe_optional_string(value.get("from") or value.get("from_date")),
        to_date=_safe_optional_string(value.get("to") or value.get("to_date")),
    )


def _safe_bool(
    value: Any,
    field_name: str,
    warnings: list[WarningItem],
) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    warnings.append(
        WarningItem(
            code=f"{field_name}_dropped",
            message="Invalid boolean metadata filter value was replaced with false.",
        )
    )
    return False


def _safe_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return _safe_string(str(value)) or None


def _safe_string(value: str) -> str:
    text = " ".join(value.split()).strip()
    if not text:
        return ""
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in SENSITIVE_MARKERS):
        return ""
    return text


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped
