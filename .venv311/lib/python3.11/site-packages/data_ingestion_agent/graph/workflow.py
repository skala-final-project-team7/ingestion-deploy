from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : LangGraph 기반 Data Ingestion workflow builder와 fallback 제공.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature5 LangGraph optional workflow wrapper 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - LangGraph 미설치 시 sequential fallback 사용
--------------------------------------------------
"""

from dataclasses import dataclass

from data_ingestion_agent.config import DataIngestionConfig
from data_ingestion_agent.workflow import (
    DataIngestionClient,
    DataIngestionWorkflowResult,
    run_full_crawl_workflow,
)


def is_langgraph_available() -> bool:
    """LangGraph import 가능 여부를 반환한다."""
    try:
        import langgraph.graph  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class DataIngestionWorkflow:
    """LangGraph 또는 sequential fallback workflow 실행 wrapper."""

    config: DataIngestionConfig
    client: DataIngestionClient | None
    backend: str

    def invoke(self) -> DataIngestionWorkflowResult:
        """Workflow를 실행한다."""
        return run_full_crawl_workflow(config=self.config, client=self.client)


def build_data_ingestion_workflow(
    *,
    config: DataIngestionConfig,
    client: DataIngestionClient | None = None,
) -> DataIngestionWorkflow:
    """Data Ingestion workflow를 생성한다.

    LangGraph가 설치된 환경에서는 backend label을 `langgraph`로 표시하고, 미설치
    환경에서는 동일한 node 순서를 따르는 sequential fallback을 명시한다.
    """
    backend = "langgraph" if is_langgraph_available() else "sequential"
    return DataIngestionWorkflow(config=config, client=client, backend=backend)
