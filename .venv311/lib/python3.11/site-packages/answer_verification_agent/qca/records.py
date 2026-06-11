from __future__ import annotations

"""QCA local record helpers for Answer Verification Agent."""

from datetime import UTC, datetime

from answer_verification_agent.schemas import (
    QCAOutput,
    QCAQualityLabel,
    VerificationOutput,
    VerificationOverallLabel,
)
from answer_verification_agent.verification.input_normalization import (
    NormalizedVerificationInput,
)


def build_qca_output(
    output: VerificationOutput,
    normalized_input: NormalizedVerificationInput,
) -> QCAOutput:
    """Build local QCA record. DB 저장은 MVP 범위가 아니다."""
    return QCAOutput(
        qca_id=f"qca-{output.verification_id}",
        conversation_id=output.conversation_id,
        generation_id=output.generation_id,
        verification_id=output.verification_id,
        question=str(normalized_input.metadata.get("query") or output.conversation_id),
        context_refs=[context.context_id for context in normalized_input.contexts],
        answer=normalized_input.answer_output.answer or "No answer text was provided.",
        overall_label=output.overall_label,
        overall_score=output.overall_score,
        quality_label=_quality_label(output.overall_label),
        created_at=datetime.now(UTC).isoformat(),
    )


def _quality_label(label: VerificationOverallLabel | str) -> QCAQualityLabel:
    if label in {VerificationOverallLabel.PASS, VerificationOverallLabel.SUPPORTED}:
        return QCAQualityLabel.ACCEPTED
    if label == VerificationOverallLabel.UNSUPPORTED:
        return QCAQualityLabel.REJECTED
    return QCAQualityLabel.NEEDS_REVIEW
