from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Ingestion Agent job schema 정의.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature1 job schema 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses 기반
--------------------------------------------------
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IngestionJob:
    """Data Ingestion full crawl job 식별자."""

    job_id: str

    def __post_init__(self) -> None:
        if not self.job_id:
            raise ValueError("job_id is required")
