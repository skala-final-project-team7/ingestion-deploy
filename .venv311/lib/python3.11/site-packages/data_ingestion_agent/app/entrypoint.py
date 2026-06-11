from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Ingestion Agent CLI/workflow 연결을 위한 최소 app entry 구조 정의.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature1 app entry skeleton 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
--------------------------------------------------
"""

from dataclasses import dataclass

from data_ingestion_agent.config import DataIngestionConfig


@dataclass(frozen=True, slots=True)
class DataIngestionApp:
    """후속 feature에서 workflow와 client를 연결할 app container."""

    config: DataIngestionConfig


def build_app(config: DataIngestionConfig) -> DataIngestionApp:
    """검증된 config를 받아 Data Ingestion app container를 생성한다."""
    config.validate()
    return DataIngestionApp(config=config)
