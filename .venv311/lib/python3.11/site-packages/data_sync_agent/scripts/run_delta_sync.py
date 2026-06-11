from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Sync Agent delta sync CLI 구현.
          CLI 인자로 외부 주입 config를 구성하고 workflow를 실행한다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature7 CLI workflow entry 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - argparse 기반 CLI
--------------------------------------------------
"""

import argparse
from collections.abc import Callable
from typing import Any

from data_sync_agent.config import DataSyncConfig
from data_sync_agent.workflow import DataSyncClient, run_data_sync_workflow


def build_parser() -> argparse.ArgumentParser:
    """Data Sync Agent delta sync CLI parser를 구성한다."""
    parser = argparse.ArgumentParser(description="Run Data Sync Agent delta sync.")
    parser.add_argument("--cloud-id", required=True)
    parser.add_argument("--access-token", required=True)
    parser.add_argument("--previous-snapshot", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--request-delay", type=float, default=0.3)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--use-admin-key", action="store_true")
    parser.add_argument("--site-url", default="")
    parser.add_argument("--admin-email", default="")
    parser.add_argument("--admin-api-token", default="")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    client: DataSyncClient | None = None,
    now: Callable[[], str] | None = None,
) -> int:
    """CLI 인자로 config를 구성하고 workflow를 실행한다."""
    args = build_parser().parse_args(argv)
    config = DataSyncConfig(
        cloud_id=args.cloud_id,
        access_token=args.access_token,
        output_dir=args.output_dir,
        previous_snapshot=args.previous_snapshot,
        request_delay_seconds=args.request_delay,
        max_retries=args.max_retries,
        timeout_seconds=args.timeout,
        use_admin_key=args.use_admin_key,
        site_url=args.site_url,
        admin_email=args.admin_email,
        admin_api_token=args.admin_api_token,
    )
    result = run_data_sync_workflow(config=config, client=client, now=now)
    summary = _summary(result.report.to_dict())
    print(
        "Data Sync Agent delta sync completed "
        f"status={summary['status']} "
        f"changed={summary['changed']} "
        f"deleted_candidates={summary['deleted_candidates']} "
        f"failed={summary['failed']} "
        f"report={summary['report']}"
    )
    return 0


def _summary(report: dict[str, Any]) -> dict[str, str | int]:
    counts = report["counts"]
    output_paths = report["output_paths"]
    return {
        "status": str(report["status"]),
        "changed": int(counts["new_pages"]) + int(counts["updated_pages"]),
        "deleted_candidates": int(counts["deleted_candidates"]),
        "failed": int(counts["failed_items"]),
        "report": str(output_paths["report"]),
    }
