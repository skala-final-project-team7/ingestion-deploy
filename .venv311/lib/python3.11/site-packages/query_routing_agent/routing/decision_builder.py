from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Query Routing Agent feature6 routing decision, search request,
          report, local JSON writer 구현.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature6 routing decision builder 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/hashlib/json/pathlib 기반
--------------------------------------------------
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from query_routing_agent.config import QueryRoutingConfig
from query_routing_agent.schemas import (
    FailedItem,
    QueryRoutingInput,
    RoutingDecision,
    RoutingReport,
    RoutingReportStatus,
    SearchRequestPayload,
    WarningItem,
)

from .filter_builder import FilterAndWeightResult
from .normalization import NormalizedRoutingInputResult
from .query_rewrite import QueryRewriteResult

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
class RoutingOutputPaths:
    """Local JSON writer output paths."""

    decision_path: Path
    search_request_path: Path
    report_path: Path
    failed_items_path: Path


def build_routing_id(routing_input: QueryRoutingInput) -> str:
    """Routing input에서 deterministic routing_id를 생성한다."""
    digest_source = "|".join(
        [
            routing_input.conversation_id,
            routing_input.user_id,
            routing_input.query,
        ]
    )
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:12]
    return f"routing-{routing_input.conversation_id}-{digest}"


def build_routing_decision(
    normalized_input: NormalizedRoutingInputResult,
    classification: Any,
    rewrite_result: QueryRewriteResult,
    filter_result: FilterAndWeightResult,
    config: QueryRoutingConfig,
    routing_id: str | None = None,
) -> RoutingDecision:
    """feature1-5 산출물을 canonical RoutingDecision으로 조립한다."""
    _validate_answer_generation_ready_fields(rewrite_result, filter_result)
    routing_input = normalized_input.routing_input
    return RoutingDecision(
        routing_id=routing_id or build_routing_id(routing_input),
        conversation_id=routing_input.conversation_id,
        user_id=routing_input.user_id,
        original_question=routing_input.original_question,
        query=routing_input.query,
        intent=classification.intent,
        task_prompt_type=filter_result.task_prompt_type,
        expanded_queries=list(rewrite_result.expanded_queries),
        metadata_filters=filter_result.metadata_filter,
        pool_weights=filter_result.pool_weights,
        confidence=classification.confidence,
        reason=classification.reason,
        warnings=_merge_warnings(
            normalized_input.warnings,
            classification.warnings,
            rewrite_result.warnings,
            filter_result.warnings,
        ),
    )


def build_search_request_payload(
    decision: RoutingDecision,
    config: QueryRoutingConfig,
) -> SearchRequestPayload:
    """RoutingDecision에서 RAG search request payload만 생성한다."""
    return SearchRequestPayload(
        routing_id=decision.routing_id,
        conversation_id=decision.conversation_id,
        user_id=decision.user_id,
        queries=list(decision.expanded_queries),
        filters=decision.metadata_filters,
        pool_weights=decision.pool_weights,
        top_k_candidates=config.top_k_candidates,
        rerank_top_k=config.rerank_top_k,
        reranking_required=True,
    )


def build_routing_report(
    decision: RoutingDecision,
    status: RoutingReportStatus = RoutingReportStatus.SUCCESS,
    job_id: str | None = None,
    created_at: str | None = None,
) -> RoutingReport:
    """RoutingDecision 기반 report를 생성한다."""
    return RoutingReport(
        job_id=job_id or f"job-{decision.routing_id}",
        routing_id=decision.routing_id,
        conversation_id=decision.conversation_id,
        status=status,
        intent=decision.intent,
        expanded_query_count=len(decision.expanded_queries),
        warnings_count=len(decision.warnings),
        created_at=created_at or _utc_now_iso(),
    )


def make_failed_item(
    item_id: str,
    reason: str,
    retryable: bool,
    error_type: str,
) -> FailedItem:
    """Safe failed item을 생성한다."""
    return FailedItem(
        item_id=_safe_string(item_id) or "redacted-item",
        reason=_safe_string(reason) or "Failure reason was redacted.",
        retryable=retryable,
        error_type=_safe_string(error_type) or "redacted_error",
    )


def write_routing_outputs(
    output_dir: str | Path,
    decision: RoutingDecision,
    search_request: SearchRequestPayload,
    report: RoutingReport,
    failed_items: list[FailedItem] | None = None,
    decision_path: str | Path | None = None,
    search_request_path: str | Path | None = None,
    report_path: str | Path | None = None,
    failed_items_path: str | Path | None = None,
) -> RoutingOutputPaths:
    """Routing 산출물을 local JSON 파일로 저장한다."""
    base_dir = Path(output_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    paths = RoutingOutputPaths(
        decision_path=(
            Path(decision_path) if decision_path else base_dir / "routing_decision.json"
        ),
        search_request_path=(
            Path(search_request_path)
            if search_request_path
            else base_dir / "search_request.json"
        ),
        report_path=Path(report_path) if report_path else base_dir / "routing_report.json",
        failed_items_path=(
            Path(failed_items_path)
            if failed_items_path
            else base_dir / "failed_items.json"
        ),
    )
    for output_path in (
        paths.decision_path,
        paths.search_request_path,
        paths.report_path,
        paths.failed_items_path,
    ):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(paths.decision_path, decision.to_dict())
    _write_json(paths.search_request_path, search_request.to_dict())
    _write_json(paths.report_path, report.to_dict())
    _write_json(
        paths.failed_items_path,
        [failed_item.to_dict() for failed_item in (failed_items or [])],
    )
    return paths


def _validate_answer_generation_ready_fields(
    rewrite_result: QueryRewriteResult,
    filter_result: FilterAndWeightResult,
) -> None:
    if not rewrite_result.expanded_queries:
        raise ValueError("expanded_queries is required")
    if not filter_result.task_prompt_type:
        raise ValueError("task_prompt_type is required")
    filter_result.metadata_filter.validate()
    filter_result.pool_weights.validate()


def _merge_warnings(*warning_groups: list[WarningItem]) -> list[WarningItem]:
    merged: list[WarningItem] = []
    seen: set[tuple[str, str]] = set()
    for warnings in warning_groups:
        for warning in warnings:
            key = (warning.code, warning.message)
            if key in seen:
                continue
            seen.add(key)
            merged.append(warning)
    return merged


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(_redact_payload(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _redact_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {key: _redact_payload(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_redact_payload(item) for item in payload]
    if isinstance(payload, str):
        return _safe_string(payload) or "<redacted>"
    return payload


def _safe_string(value: str) -> str:
    text = " ".join(value.split()).strip()
    if not text:
        return ""
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in SENSITIVE_MARKERS):
        return ""
    return text


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
