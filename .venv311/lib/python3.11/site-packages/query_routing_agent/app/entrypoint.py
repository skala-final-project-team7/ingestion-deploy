from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Query Routing Agent CLI/workflow 진입점이 공유할 app context 정의.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature1 app context skeleton 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/pathlib 기반
--------------------------------------------------
"""

from dataclasses import dataclass
from pathlib import Path

from query_routing_agent.config import QueryRoutingConfig


@dataclass(slots=True)
class QueryRoutingAppContext:
    """Query Routing Agent app entry context."""

    input_path: Path
    output_path: Path
    config: QueryRoutingConfig


def build_app_context(
    input_path: str | Path,
    output_path: str | Path,
    config: QueryRoutingConfig | None = None,
) -> QueryRoutingAppContext:
    """CLI/workflow skeleton에서 공유할 app context를 구성한다."""
    resolved_input_path = Path(input_path)
    resolved_output_path = Path(output_path)
    runtime_config = config or QueryRoutingConfig()
    runtime_config.validate()

    if not resolved_input_path.exists():
        raise FileNotFoundError(f"input file does not exist: {resolved_input_path}")
    if not resolved_input_path.is_file():
        raise ValueError(f"input path must be a file: {resolved_input_path}")

    return QueryRoutingAppContext(
        input_path=resolved_input_path,
        output_path=resolved_output_path,
        config=runtime_config,
    )
