from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : History Manager Agent 실행 설정 스키마 정의.
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


@dataclass(slots=True)
class HistoryManagerConfig:
    """History Manager Agent runtime config."""

    history_window_turns: int = 5
    max_context_chars: int = 4000
    model: str = "configurable"
    temperature: float = 0.0
    timeout_seconds: int = 30
    max_retries: int = 2
    openai_api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Config 값의 최소 유효성을 검증한다."""
        if self.history_window_turns <= 0:
            raise ValueError("history_window_turns must be greater than 0")
        if self.max_context_chars <= 0:
            raise ValueError("max_context_chars must be greater than 0")
        if not self.model:
            raise ValueError("model is required")
        if self.temperature < 0:
            raise ValueError("temperature must be greater than or equal to 0")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be greater than or equal to 0")

    def to_safe_dict(self) -> dict[str, Any]:
        """로그/report에 사용할 수 있는 key redacted dictionary를 반환한다."""
        self.validate()
        return {
            "history_window_turns": self.history_window_turns,
            "max_context_chars": self.max_context_chars,
            "model": self.model,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "openai_api_key": "<redacted>" if self.openai_api_key else None,
        }
