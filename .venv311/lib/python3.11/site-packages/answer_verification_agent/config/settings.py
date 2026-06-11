from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Verification Agent 실행 설정 스키마 정의.
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
class AnswerVerificationConfig:
    """Answer Verification Agent runtime config."""

    evaluator_model: str = "configurable"
    temperature: float = 0.0
    timeout_seconds: int = 30
    max_retries: int = 2
    evaluate_suspicious_only: bool = True
    min_overall_score: float = 0.7
    min_sentence_score: float = 0.6
    qca_output_enabled: bool = True
    openai_api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Config 값의 최소 유효성을 검증한다."""
        if not self.evaluator_model:
            raise ValueError("evaluator_model is required")
        if self.temperature < 0:
            raise ValueError("temperature must be greater than or equal to 0")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be greater than or equal to 0")
        if not isinstance(self.evaluate_suspicious_only, bool):
            raise ValueError("evaluate_suspicious_only must be a boolean")
        if not 0 <= self.min_overall_score <= 1:
            raise ValueError("min_overall_score must be between 0 and 1")
        if not 0 <= self.min_sentence_score <= 1:
            raise ValueError("min_sentence_score must be between 0 and 1")
        if not isinstance(self.qca_output_enabled, bool):
            raise ValueError("qca_output_enabled must be a boolean")

    def to_safe_dict(self) -> dict[str, Any]:
        """로그/report에 사용할 수 있는 key redacted dictionary를 반환한다."""
        self.validate()
        return {
            "evaluator_model": self.evaluator_model,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "evaluate_suspicious_only": self.evaluate_suspicious_only,
            "min_overall_score": self.min_overall_score,
            "min_sentence_score": self.min_sentence_score,
            "qca_output_enabled": self.qca_output_enabled,
            "openai_api_key": "<redacted>" if self.openai_api_key else None,
        }
