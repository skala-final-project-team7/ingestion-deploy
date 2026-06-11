from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Query Routing Agent 실행 설정 스키마 정의.
          OPENAI_API_KEY는 외부 주입으로만 받고 safe serialization에서 redaction한다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature1 config schema 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses 기반
--------------------------------------------------
"""

from dataclasses import dataclass, field
from typing import Any

from query_routing_agent.schemas import PoolWeights


@dataclass(slots=True)
class QueryRoutingConfig:
    """Query Routing Agent runtime config."""

    model: str = "configurable"
    temperature: float = 0.0
    timeout_seconds: int = 30
    max_retries: int = 2
    default_query_count: int = 3
    max_query_count: int = 5
    top_k_candidates: int = 20
    rerank_top_k: int = 5
    default_pool_weights: PoolWeights = field(default_factory=PoolWeights)
    openai_api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.default_pool_weights, PoolWeights):
            self.default_pool_weights = PoolWeights(
                title=float(self.default_pool_weights.get("title", 0.25)),
                content=float(self.default_pool_weights.get("content", 0.6)),
                label=float(self.default_pool_weights.get("label", 0.15)),
            )
        self.validate()

    def validate(self) -> None:
        """Config 값의 최소 유효성을 검증한다."""
        if not self.model:
            raise ValueError("model is required")
        if self.temperature < 0:
            raise ValueError("temperature must be greater than or equal to 0")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be greater than or equal to 0")
        if self.default_query_count <= 0:
            raise ValueError("default_query_count must be greater than 0")
        if self.max_query_count <= 0:
            raise ValueError("max_query_count must be greater than 0")
        if self.default_query_count > self.max_query_count:
            raise ValueError("default_query_count cannot exceed max_query_count")
        if self.top_k_candidates <= 0:
            raise ValueError("top_k_candidates must be greater than 0")
        if self.rerank_top_k <= 0:
            raise ValueError("rerank_top_k must be greater than 0")
        self.default_pool_weights.validate()

    def to_safe_dict(self) -> dict[str, Any]:
        """로그/report에 사용할 수 있는 key redacted dictionary를 반환한다."""
        self.validate()
        return {
            "model": self.model,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "default_query_count": self.default_query_count,
            "max_query_count": self.max_query_count,
            "top_k_candidates": self.top_k_candidates,
            "rerank_top_k": self.rerank_top_k,
            "default_pool_weights": self.default_pool_weights.to_dict(),
            "openai_api_key": "<redacted>" if self.openai_api_key else None,
        }
