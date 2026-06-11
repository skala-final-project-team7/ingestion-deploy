from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : History Manager Agent CLI/workflow 진입점이 공유할 app context 구성.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature1 app context skeleton 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses 기반
--------------------------------------------------
"""

from dataclasses import dataclass

from history_manager_agent.config import HistoryManagerConfig


@dataclass(frozen=True, slots=True)
class HistoryManagerAppContext:
    """후속 workflow/CLI 구현에서 공유할 app context."""

    config: HistoryManagerConfig


def build_app_context(config: HistoryManagerConfig) -> HistoryManagerAppContext:
    """검증된 config로 app context를 생성한다."""
    config.validate()
    return HistoryManagerAppContext(config=config)
