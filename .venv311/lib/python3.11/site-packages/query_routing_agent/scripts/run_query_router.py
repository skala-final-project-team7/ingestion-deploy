from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Query Routing Agent CLI entrypoint.
          History Manager output을 routing workflow로 처리한다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature1 CLI skeleton 구현
  - 2026-05-15, feature7 구현, workflow 수동 실행 CLI로 확장
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 argparse/json 기반
--------------------------------------------------
"""

import argparse
from pathlib import Path
from typing import Sequence

from query_routing_agent.config import QueryRoutingConfig
from query_routing_agent.llm import FakeRoutingLLMProvider, OpenAIRoutingLLMProvider
from query_routing_agent.schemas import IntentLabel
from query_routing_agent.workflow import run_query_routing_workflow


def build_parser() -> argparse.ArgumentParser:
    """CLI argument parser를 구성한다."""
    parser = argparse.ArgumentParser(description="Run Query Routing workflow.")
    parser.add_argument("--input", required=True, help="History Manager output JSON path.")
    parser.add_argument("--output", required=True, help="Routing decision output JSON path.")
    parser.add_argument("--report-output", help="Routing report output JSON path.")
    parser.add_argument("--failed-output", help="Failed item output JSON path.")
    parser.add_argument(
        "--provider",
        choices=("openai", "fake"),
        default="fake",
        help="LLM provider. OpenAI is opt-in and requires external key injection.",
    )
    parser.add_argument(
        "--fake-intent",
        choices=[intent.value for intent in IntentLabel],
        default=IntentLabel.UNKNOWN.value,
        help="Intent emitted by the fake provider.",
    )
    parser.add_argument("--model", default="configurable", help="Model name.")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--default-query-count", type=int, default=3)
    parser.add_argument("--max-query-count", type=int, default=5)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI workflow를 실행한다. Search request payload 생성까지만 수행한다."""
    args = build_parser().parse_args(argv)
    config = QueryRoutingConfig(
        model=args.model,
        temperature=args.temperature,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
        default_query_count=args.default_query_count,
        max_query_count=args.max_query_count,
    )
    provider = _build_provider(args.provider, args.fake_intent, config)
    result = run_query_routing_workflow(
        input_path=Path(args.input),
        output_path=Path(args.output),
        report_output_path=Path(args.report_output) if args.report_output else None,
        failed_output_path=Path(args.failed_output) if args.failed_output else None,
        config=config,
        provider=provider,
    )

    print(
        "Query Routing input validated; workflow completed: "
        f"status={result.status} execution_mode={result.execution_mode}"
    )
    return 0 if result.status == "success" else 1


def _build_provider(
    provider_name: str,
    fake_intent: str,
    config: QueryRoutingConfig,
) -> FakeRoutingLLMProvider | OpenAIRoutingLLMProvider:
    if provider_name == "fake":
        return FakeRoutingLLMProvider(
            {
                "intent": fake_intent,
                "confidence": 0.82,
                "reason": "Synthetic fake provider response.",
                "expanded_queries": [
                    "synthetic routing query",
                    "synthetic routing troubleshooting",
                    "synthetic routing guide",
                ],
            }
        )
    return OpenAIRoutingLLMProvider.from_config(config)


if __name__ == "__main__":
    raise SystemExit(main())
