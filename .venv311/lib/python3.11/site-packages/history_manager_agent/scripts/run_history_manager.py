from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : History Manager Agent CLI 실행 진입점 구현.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature1 CLI skeleton 구현
  - 2026-05-15, workflow 연결, feature6 CLI 실행 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - argparse/json/pathlib 기반
--------------------------------------------------
"""

import argparse
import os
from pathlib import Path

from history_manager_agent.config import HistoryManagerConfig
from history_manager_agent.llm import FakeHistoryLLMProvider, OpenAIHistoryLLMProvider
from history_manager_agent.workflow import run_history_manager_workflow


def build_parser() -> argparse.ArgumentParser:
    """History Manager Agent CLI parser를 구성한다."""
    parser = argparse.ArgumentParser(description="Run History Manager Agent.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report-output")
    parser.add_argument("--provider", choices=["fake", "openai"], default="fake")
    parser.add_argument(
        "--fake-decision",
        choices=["follow_up", "new_topic", "ambiguous"],
        default="new_topic",
    )
    parser.add_argument("--fake-confidence", type=float, default=0.9)
    parser.add_argument("--history-window-turns", type=int, default=5)
    parser.add_argument("--max-context-chars", type=int, default=4000)
    parser.add_argument("--model", default="configurable")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-retries", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 인자로 History Manager workflow를 실행한다."""
    args = build_parser().parse_args(argv)
    config = HistoryManagerConfig(
        history_window_turns=args.history_window_turns,
        max_context_chars=args.max_context_chars,
        model=args.model,
        temperature=args.temperature,
        timeout_seconds=args.timeout,
        max_retries=args.max_retries,
    )
    provider = _build_provider(args, config)
    result = run_history_manager_workflow(
        input_path=Path(args.input),
        output_path=Path(args.output),
        report_output_path=Path(args.report_output) if args.report_output else None,
        config=config,
        provider=provider,
    )
    print(
        "History Manager Agent workflow completed "
        f"status={result.status} "
        f"mode={result.execution_mode} "
        f"output={args.output}"
    )
    return 0 if result.status != "failed" else 1


def _build_provider(
    args: argparse.Namespace,
    config: HistoryManagerConfig,
) -> FakeHistoryLLMProvider | OpenAIHistoryLLMProvider:
    if args.provider == "openai":
        return OpenAIHistoryLLMProvider.from_config(config, env=os.environ)
    return FakeHistoryLLMProvider(
        {
            "history_decision": args.fake_decision,
            "confidence": args.fake_confidence,
            "reason": "Synthetic CLI fake provider classification.",
        }
    )
