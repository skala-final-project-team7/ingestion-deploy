from __future__ import annotations

"""Regeneration recommendation helpers for Answer Verification Agent."""

from answer_verification_agent.schemas import RegenerationRequest


def build_regeneration_request(
    generation_id: str,
    unsupported_sentence_ids: list[str],
    unsupported_claims: list[dict[str, object]],
) -> RegenerationRequest | None:
    """Build payload only. Answer Generation Agent 재호출은 MVP 범위가 아니다."""
    if not unsupported_sentence_ids:
        return None
    claim_texts = [
        str(claim.get("text") or claim.get("reason") or "")
        for claim in unsupported_claims
        if str(claim.get("text") or claim.get("reason") or "").strip()
    ]
    guidance = (
        "Regenerate the answer with unsupported claims removed or grounded in cited "
        f"context. Unsupported claims: {'; '.join(claim_texts) or 'see sentence ids'}"
    )
    return RegenerationRequest(
        target_generation_id=generation_id,
        unsupported_sentence_ids=unsupported_sentence_ids,
        guidance=guidance,
    )
