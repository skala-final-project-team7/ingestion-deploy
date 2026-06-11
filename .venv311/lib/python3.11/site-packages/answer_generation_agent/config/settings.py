from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Generation Agent 실행 설정 스키마 정의.
          OPENAI_API_KEY는 외부 주입으로만 받고 safe serialization에서 redaction한다.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature1 config schema 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses 기반
--------------------------------------------------
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AnswerGenerationConfig:
    """Answer Generation Agent runtime config."""

    model: str = "configurable"
    fallback_model: str = "configurable"
    temperature: float = 0.2
    timeout_seconds: int = 45
    max_retries: int = 2
    max_contexts: int = 5
    max_answer_sentences: int = 8
    streaming_supported: bool = False
    openai_api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Config 값의 최소 유효성을 검증한다."""
        if not self.model:
            raise ValueError("model is required")
        if not self.fallback_model:
            raise ValueError("fallback_model is required")
        if self.temperature < 0:
            raise ValueError("temperature must be greater than or equal to 0")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be greater than or equal to 0")
        if self.max_contexts <= 0:
            raise ValueError("max_contexts must be greater than 0")
        if self.max_answer_sentences <= 0:
            raise ValueError("max_answer_sentences must be greater than 0")
        if not isinstance(self.streaming_supported, bool):
            raise ValueError("streaming_supported must be a boolean")

    def to_safe_dict(self) -> dict[str, Any]:
        """로그/report에 사용할 수 있는 key redacted dictionary를 반환한다."""
        self.validate()
        return {
            "model": self.model,
            "fallback_model": self.fallback_model,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "max_contexts": self.max_contexts,
            "max_answer_sentences": self.max_answer_sentences,
            "streaming_supported": self.streaming_supported,
            "openai_api_key": "<redacted>" if self.openai_api_key else None,
        }
