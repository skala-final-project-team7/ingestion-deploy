from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Verification Agent evaluator prompt builder.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature6 evaluator prompt payload 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/re 기반
--------------------------------------------------
"""

import re
from dataclasses import dataclass
from typing import Any

from answer_verification_agent.schemas._serialization import to_primitive
from answer_verification_agent.verification.input_normalization import (
    NormalizedContext,
)
from answer_verification_agent.verification.suspicious_selector import (
    SuspiciousSentenceTarget,
)

_SENSITIVE_PATTERNS = [
    re.compile(r"OPENAI_API_KEY\s*=\s*[^,\s]+", re.IGNORECASE),
    re.compile(r"Authorization:\s*Bearer\s+[^,\s]+", re.IGNORECASE),
    re.compile(r"Bearer\s+[^,\s]+", re.IGNORECASE),
]


@dataclass(slots=True)
class EvaluatorPrompt:
    """LLM evaluator prompt payload."""

    system_prompt: str
    user_prompt: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return _redact_payload(to_primitive(self))


def build_evaluator_prompt(
    target: SuspiciousSentenceTarget,
    contexts: list[NormalizedContext],
) -> EvaluatorPrompt:
    """Sentence와 cited context를 evaluator prompt로 조립한다."""
    cited_contexts = [
        context
        for context in contexts
        if context.context_id in set(target.matched_context_ids + target.citations)
    ]
    context_lines = [
        _format_context(context)
        for context in cited_contexts
    ]
    if not context_lines:
        context_lines.append("No valid cited context was available.")

    system_prompt = (
        "You are an answer verification evaluator. Evaluate only whether the "
        "sentence is supported by the provided cited contexts. Return JSON with "
        "label, score, reason, and unsupported_claims. Allowed labels are "
        "SUPPORTED, UNSUPPORTED, LOW_CONFIDENCE."
    )
    user_prompt = "\n".join(
        [
            f"Sentence ID: {target.sentence_id}",
            f"Sentence: {target.text}",
            f"Failed rules: {', '.join(target.failed_rules) or 'none'}",
            f"Suspicious reasons: {', '.join(target.reasons) or 'none'}",
            "Cited contexts:",
            "\n".join(context_lines),
        ]
    )
    return EvaluatorPrompt(
        system_prompt=_redact_text(system_prompt),
        user_prompt=_redact_text(user_prompt),
        metadata={
            "sentence_id": target.sentence_id,
            "context_ids": [context.context_id for context in cited_contexts],
            "failed_rules": list(target.failed_rules),
            "suspicious_reasons": list(target.reasons),
        },
    )


def _format_context(context: NormalizedContext) -> str:
    snippet = context.content[:1200]
    return (
        f"- context_id={context.context_id}; title={context.title}; "
        f"source_url={context.source_url}; content={snippet}"
    )


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
