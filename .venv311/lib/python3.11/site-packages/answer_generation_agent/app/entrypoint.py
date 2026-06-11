from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Generation Agent CLI/app entry context skeleton.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature1 app context 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/pathlib 기반
--------------------------------------------------
"""

from dataclasses import dataclass
from pathlib import Path

from answer_generation_agent.config import AnswerGenerationConfig


@dataclass(slots=True)
class AnswerGenerationAppContext:
    """CLI entrypoint가 공유할 app context."""

    input_path: Path
    output_path: Path
    config: AnswerGenerationConfig


def build_app_context(
    input_path: str | Path,
    output_path: str | Path,
    config: AnswerGenerationConfig,
) -> AnswerGenerationAppContext:
    """CLI/app context를 생성한다."""
    config.validate()
    return AnswerGenerationAppContext(
        input_path=Path(input_path),
        output_path=Path(output_path),
        config=config,
    )
