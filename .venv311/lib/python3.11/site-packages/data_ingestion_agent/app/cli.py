from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Ingestion Agent full crawl CLI 진입점 구현.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature5 CLI workflow 연결 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - LangGraph optional fallback workflow 기준
--------------------------------------------------
"""

import argparse
from pathlib import Path
from typing import Sequence

from data_ingestion_agent.config import DataIngestionConfig
from data_ingestion_agent.confluence import ConfluenceClient
from data_ingestion_agent.graph import build_data_ingestion_workflow
from data_ingestion_agent.workflow import DataIngestionClient


def build_parser() -> argparse.ArgumentParser:
    """CLI argument parser를 생성한다."""
    parser = argparse.ArgumentParser(description="Run Data Ingestion full crawl.")
    parser.add_argument("--cloud-id", required=True)
    parser.add_argument("--access-token", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--request-delay", type=float, default=0.3)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--use-admin-key", action="store_true")
    parser.add_argument("--site-url", default="")
    parser.add_argument("--admin-email", default="")
    parser.add_argument("--admin-api-token", default="")
    return parser


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    client: DataIngestionClient | None = None,
) -> int:
    """CLI 인자를 config로 변환하고 workflow를 실행한다."""
    args = build_parser().parse_args(argv)
    config = DataIngestionConfig(
        cloud_id=args.cloud_id,
        access_token=args.access_token,
        output_dir=args.output_dir,
        request_delay_seconds=args.request_delay,
        max_retries=args.max_retries,
        timeout_seconds=args.timeout,
        use_admin_key=args.use_admin_key,
        site_url=args.site_url,
        admin_email=args.admin_email,
        admin_api_token=args.admin_api_token,
    )
    workflow = build_data_ingestion_workflow(
        config=config,
        client=client or ConfluenceClient(config=config),
    )
    result = workflow.invoke()
    print(f"Data Ingestion workflow completed: {result.summary()}")
    if result.write_result is not None:
        print(f"Output report: {result.write_result.report_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Console script main function."""
    return run_cli(argv)
