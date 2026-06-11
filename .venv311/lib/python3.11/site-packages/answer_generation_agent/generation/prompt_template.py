from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Generation Agent task prompt template builder.
          normalized input과 Top context를 LLM provider용 prompt payload로 조립한다.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature3 prompt template builder 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses 기반
--------------------------------------------------
"""

from dataclasses import dataclass, field
from typing import Any

from answer_generation_agent.generation.input_normalization import (
    NormalizedGenerationInputResult,
)
from answer_generation_agent.schemas import TaskPromptType, TopContext, WarningItem

_TASK_INSTRUCTIONS: dict[TaskPromptType, str] = {
    TaskPromptType.TIMELINE: (
        "Task Prompt Type: timeline\n"
        "목적: 장애 대응.\n"
        "답변에는 상황 요약, 시간/단계 흐름, 조치 순서, 근거를 포함한다."
    ),
    TaskPromptType.STEP_BY_STEP: (
        "Task Prompt Type: step_by_step\n"
        "목적: 운영 가이드.\n"
        "답변에는 단계별 절차, 주의사항, 확인 방법을 포함한다."
    ),
    TaskPromptType.EVIDENCE_FIRST: (
        "Task Prompt Type: evidence_first\n"
        "목적: 정책·절차.\n"
        "답변에는 근거 문서/조항 우선, 결론, 예외/주의사항을 포함한다."
    ),
    TaskPromptType.HISTORY_SUMMARY: (
        "Task Prompt Type: history_summary\n"
        "목적: 이력 조회.\n"
        "답변에는 변경/처리 이력 요약, 날짜/대상/결과를 포함한다."
    ),
    TaskPromptType.GENERAL: (
        "Task Prompt Type: general\n"
        "목적: 일반 질문.\n"
        "답변은 간결한 직접 답변과 근거 출처를 포함한다."
    ),
}

_SENSITIVE_KEYWORDS = (
    "OPENAI_API_KEY",
    "Authorization",
    "api key",
    "API key",
    "token",
    "secret",
    "synthetic-marker",
)


@dataclass(slots=True)
class PromptPayload:
    """LLM provider가 사용할 prompt payload."""

    task_prompt_type: str
    system_prompt: str
    developer_prompt: str
    user_prompt: str
    context_count: int
    warnings: list[WarningItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.warnings = [
            warning
            if isinstance(warning, WarningItem)
            else WarningItem(
                code=str(warning.get("code") or "prompt_warning"),
                message=str(warning.get("message") or "Prompt warning."),
            )
            for warning in self.warnings
        ]

    def combined_text(self) -> str:
        """system/developer/user prompt를 하나의 문자열로 반환한다."""
        return "\n\n".join(
            [self.system_prompt, self.developer_prompt, self.user_prompt]
        )

    def to_dict(self) -> dict[str, Any]:
        """safe serialization을 반환한다."""
        return {
            "task_prompt_type": self.task_prompt_type,
            "system_prompt": _redact_text(self.system_prompt),
            "developer_prompt": _redact_text(self.developer_prompt),
            "user_prompt": _redact_text(self.user_prompt),
            "context_count": self.context_count,
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


def build_prompt_payload(
    normalized_input: NormalizedGenerationInputResult,
    *,
    max_context_chars: int = 12_000,
) -> PromptPayload:
    """Normalized generation input에서 task-specific prompt payload를 생성한다.

    Args:
        normalized_input: feature2 normalization result.
        max_context_chars: prompt에 포함할 context content 총 길이 상한.

    Returns:
        PromptPayload.
    """
    warnings = list(normalized_input.warnings)
    task_prompt_type = _resolve_task_prompt_type(
        normalized_input.generation_input.routing_decision.task_prompt_type,
        warnings,
    )
    context_block = _format_contexts(
        normalized_input.normalized_contexts,
        max_context_chars=max_context_chars,
        warnings=warnings,
    )
    system_prompt = _build_system_prompt()
    developer_prompt = _build_developer_prompt(task_prompt_type)
    user_prompt = _build_user_prompt(
        query=normalized_input.generation_input.routing_decision.query,
        original_question=(
            normalized_input.generation_input.routing_decision.original_question
        ),
        context_block=context_block,
        has_context=bool(normalized_input.normalized_contexts),
    )
    if normalized_input.insufficient_context_candidate:
        warnings.append(
            WarningItem(
                code="insufficient_context_candidate",
                message="No usable context is available for answer generation.",
            )
        )

    return PromptPayload(
        task_prompt_type=task_prompt_type.value,
        system_prompt=_redact_text(system_prompt),
        developer_prompt=_redact_text(developer_prompt),
        user_prompt=_redact_text(user_prompt),
        context_count=len(normalized_input.normalized_contexts),
        warnings=warnings,
    )


def _resolve_task_prompt_type(
    task_prompt_type: TaskPromptType | str,
    warnings: list[WarningItem],
) -> TaskPromptType:
    if isinstance(task_prompt_type, TaskPromptType):
        return task_prompt_type
    try:
        return TaskPromptType(task_prompt_type)
    except ValueError:
        warnings.append(
            WarningItem(
                code="unsupported_task_prompt_type",
                message="Unsupported task prompt type was replaced with general.",
            )
        )
        return TaskPromptType.GENERAL


def _build_system_prompt() -> str:
    return "\n".join(
        [
            "You are the Answer Generation Agent in a RAG pipeline.",
            "제공된 context 밖의 사실을 단정하지 않는다.",
            "가능한 한 입력 context 안에서 답변을 구성한다.",
            "근거가 부족한 부분은 제한 사항으로 표시한다.",
            "context가 존재하면 근거 있는 범위에서 최대한 답변한다.",
            "모든 핵심 문장은 sentence-level citation을 포함해야 한다.",
            "citation은 반드시 제공된 context_id만 참조한다.",
        ]
    )


def _build_developer_prompt(task_prompt_type: TaskPromptType) -> str:
    return "\n\n".join(
        [
            _TASK_INSTRUCTIONS[task_prompt_type],
            _structured_output_instruction(),
        ]
    )


def _structured_output_instruction() -> str:
    return (
        "Answer Verification Agent가 검증 가능한 JSON/schema output으로 답변한다.\n"
        "출력 schema 예시:\n"
        "{\n"
        '  "answer": "string",\n'
        '  "sentences": [\n'
        '    {"sentence_id": "s1", "text": "string", "citations": ["context_id"]}\n'
        "  ],\n"
        '  "unsupported_gaps": ["context로 확인할 수 없는 제한 사항"]\n'
        "}\n"
        "sentences의 citations는 Top context의 context_id만 사용한다."
    )


def _build_user_prompt(
    *,
    query: str,
    original_question: str,
    context_block: str,
    has_context: bool,
) -> str:
    context_instruction = (
        "아래 Top context만 근거로 사용한다."
        if has_context
        else "사용 가능한 context가 없다. 답변 가능 범위를 제한 사항으로 표시한다."
    )
    return "\n\n".join(
        [
            f"Original question: {_redact_text(original_question)}",
            f"Contextualized query: {_redact_text(query)}",
            context_instruction,
            context_block,
        ]
    )


def _format_contexts(
    contexts: list[TopContext],
    *,
    max_context_chars: int,
    warnings: list[WarningItem],
) -> str:
    if not contexts:
        return "Top contexts: []"
    remaining_chars = max_context_chars
    formatted_contexts: list[str] = []
    for index, context in enumerate(contexts, start=1):
        safe_content, was_truncated = _truncate_text(
            _redact_text(context.content),
            remaining_chars,
        )
        if was_truncated:
            warnings.append(
                WarningItem(
                    code="context_truncated",
                    message="A top context was truncated for prompt length guard.",
                )
            )
        remaining_chars = max(0, remaining_chars - len(safe_content))
        formatted_contexts.append(
            "\n".join(
                [
                    f"[Context {index}]",
                    f"context_id: {_redact_text(context.context_id)}",
                    f"title: {_redact_text(context.title)}",
                    f"space_key: {_redact_text(context.space_key)}",
                    f"source_url: {_redact_text(context.source_url)}",
                    f"score={context.score}",
                    f"rerank_score={context.rerank_score}",
                    f"content: {safe_content}",
                ]
            )
        )
        if remaining_chars <= 0:
            break
    return "\n\n".join(formatted_contexts)


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0:
        return "", True
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _redact_text(text: str) -> str:
    redacted = text
    for marker in _SENSITIVE_KEYWORDS:
        redacted = redacted.replace(marker, "<redacted>")
    return redacted
