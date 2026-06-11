from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Generation Agent canonical AnswerOutput 조립 및 local JSON writer 구현.
          feature6 범위에서 Answer Verification Agent가 소비 가능한 output/report를 생성한다.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature6 answer output builder 구현
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

from answer_generation_agent.generation.answer_generation import (
    AnswerGenerationResult,
    AnswerProviderError,
)
from answer_generation_agent.generation.citation_mapping import CitationMappingResult
from answer_generation_agent.generation.input_normalization import (
    NormalizedGenerationInputResult,
)
from answer_generation_agent.schemas import (
    AnswerOutput,
    AnswerStatus,
    FailedItem,
    GenerationReport,
    GenerationReportStatus,
    StreamChunk,
    StreamChunkType,
    StreamingOutput,
    WarningItem,
)

_REDACTION_MARKERS = (
    "OPENAI_API_KEY",
    "Authorization",
    "api key",
    "API key",
    "secret",
    "token",
    "synthetic-marker",
)


@dataclass(slots=True)
class AnswerOutputWriteResult:
    """Local JSON writer result."""

    output_path: Path
    report_path: Path
    failed_path: Path | None = None


def build_generation_id(normalized_input: NormalizedGenerationInputResult) -> str:
    """Generation input의 추적 가능한 값으로 deterministic generation id를 생성한다."""
    routing = normalized_input.generation_input.routing_decision
    seed = "|".join(
        [
            normalized_input.generation_input.conversation_id,
            normalized_input.generation_input.user_id,
            routing.routing_id,
            routing.query,
        ]
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"generation-{digest}"


def build_answer_output(
    *,
    normalized_input: NormalizedGenerationInputResult,
    generation_result: AnswerGenerationResult,
    citation_result: CitationMappingResult,
) -> AnswerOutput:
    """Normalized input, generation result, citation mapping result를 AnswerOutput으로 조립한다."""
    answer_status = _answer_status(generation_result.answer_status)
    generation_id = build_generation_id(normalized_input)
    answer_text = _answer_text_for_status(
        answer_status=answer_status,
        answer_text=generation_result.answer_text,
        citation_result=citation_result,
    )
    warnings = _merge_warnings(
        normalized_input.warnings,
        generation_result.warnings,
        citation_result.warnings,
        _status_warnings(answer_status),
    )
    unsupported_gaps = _safe_string_list(generation_result.unsupported_gaps)

    return AnswerOutput(
        generation_id=generation_id,
        conversation_id=normalized_input.generation_input.conversation_id,
        user_id=normalized_input.generation_input.user_id,
        answer_status=answer_status,
        answer=answer_text,
        sentences=citation_result.sentences,
        sources=citation_result.sources,
        used_context_ids=list(citation_result.used_context_ids),
        routing=_routing_metadata(normalized_input),
        model=_safe_model(generation_result.model),
        confidence=normalized_input.generation_input.routing_decision.confidence,
        insufficient_context=answer_status == AnswerStatus.INSUFFICIENT_CONTEXT,
        unsupported_gaps=unsupported_gaps,
        streaming=_streaming_output(generation_id=generation_id, answer_text=answer_text),
        warnings=warnings,
    )


def build_failed_answer_output(
    *,
    normalized_input: NormalizedGenerationInputResult,
    error: Exception,
    model: str,
) -> AnswerOutput:
    """Provider/generation failure를 safe failed AnswerOutput으로 변환한다."""
    generation_id = build_generation_id(normalized_input)
    warning = WarningItem(
        code="answer_generation_failed",
        message=_safe_message(str(error) or "Answer generation failed."),
    )
    answer_text = "Answer generation failed."
    return AnswerOutput(
        generation_id=generation_id,
        conversation_id=normalized_input.generation_input.conversation_id,
        user_id=normalized_input.generation_input.user_id,
        answer_status=AnswerStatus.FAILED,
        answer=answer_text,
        sentences=[],
        sources=[],
        used_context_ids=[],
        routing=_routing_metadata(normalized_input),
        model=_safe_model(model),
        confidence=normalized_input.generation_input.routing_decision.confidence,
        insufficient_context=False,
        unsupported_gaps=[],
        streaming=_streaming_output(generation_id=generation_id, answer_text=answer_text),
        warnings=_merge_warnings(normalized_input.warnings, [warning]),
    )


def build_generation_report(
    *,
    answer_output: AnswerOutput,
    normalized_input: NormalizedGenerationInputResult,
    created_at: str | None = None,
) -> GenerationReport:
    """AnswerOutput 기반 generation report를 생성한다."""
    answer_status = _answer_status(answer_output.answer_status)
    report_status = _report_status(answer_status)
    return GenerationReport(
        job_id=f"job-{answer_output.generation_id.removeprefix('generation-')}",
        generation_id=answer_output.generation_id,
        conversation_id=answer_output.conversation_id,
        status=report_status,
        answer_status=answer_status,
        context_count=len(normalized_input.normalized_contexts),
        used_context_count=len(answer_output.used_context_ids),
        sentence_count=len(answer_output.sentences),
        citation_count=sum(len(sentence.citations) for sentence in answer_output.sentences),
        warnings_count=len(answer_output.warnings),
        created_at=created_at or _utc_now(),
    )


def build_failed_item(*, item_id: str, error: Exception) -> FailedItem:
    """Safe failed item을 생성한다."""
    retryable = bool(getattr(error, "retryable", False))
    error_type = str(getattr(error, "error_type", "generation_error") or "generation_error")
    reason = _safe_message(str(error) or "Answer generation failed.")
    return FailedItem(
        item_id=_redact_text(item_id),
        reason=reason,
        retryable=retryable,
        error_type=_redact_text(error_type),
    )


def write_answer_outputs(
    *,
    output_dir: Path,
    answer_output: AnswerOutput,
    report: GenerationReport,
    failed_item: FailedItem | None = None,
) -> AnswerOutputWriteResult:
    """Answer output/report/failed JSON 파일을 local output directory에 저장한다."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "answer_output.json"
    report_path = output_dir / "generation_report.json"
    failed_path = output_dir / "failed_items.json" if failed_item is not None else None

    _write_json(output_path, answer_output.to_dict())
    _write_json(report_path, report.to_dict())
    if failed_path is not None:
        _write_json(failed_path, {"failed_items": [failed_item.to_dict()]})

    return AnswerOutputWriteResult(
        output_path=output_path,
        report_path=report_path,
        failed_path=failed_path,
    )


def _answer_status(value: str | AnswerStatus) -> AnswerStatus:
    if isinstance(value, AnswerStatus):
        return value
    try:
        return AnswerStatus(str(value))
    except ValueError:
        return AnswerStatus.FAILED


def _answer_text_for_status(
    *,
    answer_status: AnswerStatus,
    answer_text: str,
    citation_result: CitationMappingResult,
) -> str:
    if answer_status == AnswerStatus.INSUFFICIENT_CONTEXT:
        return "Insufficient context to generate a supported answer."
    if answer_status == AnswerStatus.FAILED:
        return "Answer generation failed."
    safe_answer = _redact_text(answer_text).strip()
    if safe_answer:
        return safe_answer
    sentence_text = " ".join(sentence.text for sentence in citation_result.sentences).strip()
    return sentence_text or "Answer generation completed without supported sentence text."


def _status_warnings(answer_status: AnswerStatus) -> list[WarningItem]:
    if answer_status == AnswerStatus.INSUFFICIENT_CONTEXT:
        return [
            WarningItem(
                code="insufficient_context",
                message="No usable context is available for supported answer generation.",
            )
        ]
    if answer_status == AnswerStatus.FAILED:
        return [
            WarningItem(
                code="answer_generation_failed",
                message="Answer generation failed.",
            )
        ]
    return []


def _routing_metadata(normalized_input: NormalizedGenerationInputResult) -> dict[str, Any]:
    routing = normalized_input.generation_input.routing_decision
    return {
        "routing_id": _redact_text(routing.routing_id),
        "intent": _redact_text(routing.intent),
        "task_prompt_type": str(routing.task_prompt_type),
    }


def _streaming_output(*, generation_id: str, answer_text: str) -> StreamingOutput:
    chunks = []
    if answer_text:
        chunks.append(
            StreamChunk(
                generation_id=generation_id,
                chunk_index=0,
                chunk_type=StreamChunkType.TEXT,
                content=_redact_text(answer_text),
                metadata={"interface_only": True},
            )
        )
    return StreamingOutput(streaming_supported=False, stream_chunks=chunks)


def _report_status(answer_status: AnswerStatus) -> GenerationReportStatus:
    if answer_status == AnswerStatus.FAILED:
        return GenerationReportStatus.FAILED
    if answer_status == AnswerStatus.INSUFFICIENT_CONTEXT:
        return GenerationReportStatus.PARTIAL_SUCCESS
    return GenerationReportStatus.SUCCESS


def _merge_warnings(*groups: list[WarningItem]) -> list[WarningItem]:
    warnings: list[WarningItem] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for warning in group:
            safe_warning = WarningItem(
                code=_redact_text(warning.code),
                message=_safe_message(warning.message),
            )
            key = (safe_warning.code, safe_warning.message)
            if key in seen:
                continue
            seen.add(key)
            warnings.append(safe_warning)
    return warnings


def _safe_string_list(values: list[str]) -> list[str]:
    return [_redact_text(str(value)) for value in values if str(value).strip()]


def _safe_model(model: str) -> str:
    return _redact_text(model).strip() or "unknown-model"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    safe_payload = _sanitize_value(payload)
    path.write_text(
        json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_value(item)
            for key, item in value.items()
            if not _is_sensitive_key(str(key))
        }
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(marker in normalized for marker in ("api_key", "authorization", "token", "secret"))


def _safe_message(message: str) -> str:
    redacted = _redact_text(message)
    return redacted or "Generation error."


def _redact_text(text: str) -> str:
    redacted = text
    for marker in _REDACTION_MARKERS:
        redacted = redacted.replace(marker, "<redacted>")
    return redacted


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
