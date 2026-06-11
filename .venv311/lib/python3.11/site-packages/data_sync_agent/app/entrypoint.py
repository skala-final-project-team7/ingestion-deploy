from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Sync Agent CLI/workflow 진입점이 공유할 최소 app context 구성.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature1 app entry skeleton 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses 기반
--------------------------------------------------
"""

from dataclasses import dataclass

from data_sync_agent.config import DataSyncConfig


@dataclass(frozen=True, slots=True)
class DataSyncAppContext:
    """후속 workflow/CLI 구현에서 공유할 최소 app context."""

    config: DataSyncConfig


def build_app_context(config: DataSyncConfig) -> DataSyncAppContext:
    """검증된 config로 Data Sync Agent app context를 생성한다."""
    config.validate()
    return DataSyncAppContext(config=config)
