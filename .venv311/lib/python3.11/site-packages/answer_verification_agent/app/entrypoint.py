from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Verification Agent CLI/app skeleton context 구성.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature1 validation-only app context 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 pathlib/dataclasses 기반
--------------------------------------------------
"""

from dataclasses import dataclass
from pathlib import Path

from answer_verification_agent.config import AnswerVerificationConfig


@dataclass(slots=True)
class AppContext:
    """CLI/app skeleton context."""

    input_path: Path
    output_path: Path
    config: AnswerVerificationConfig


def build_app_context(
    input_path: Path,
    output_path: Path,
    config: AnswerVerificationConfig | None = None,
) -> AppContext:
    """실제 verification workflow 실행 전 validation-only context를 구성한다."""
    if not input_path:
        raise ValueError("input_path is required")
    if not output_path:
        raise ValueError("output_path is required")
    return AppContext(
        input_path=Path(input_path),
        output_path=Path(output_path),
        config=config or AnswerVerificationConfig(),
    )
