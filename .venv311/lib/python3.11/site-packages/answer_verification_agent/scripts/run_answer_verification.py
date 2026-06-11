from __future__ import annotations

"""Answer Verification Agent CLI entrypoint."""

import argparse
from pathlib import Path
from typing import Sequence

from answer_verification_agent.config import AnswerVerificationConfig
from answer_verification_agent.workflow import run_verification_workflow


def build_parser() -> argparse.ArgumentParser:
    """CLI parser를 생성한다."""
    parser = argparse.ArgumentParser(description="Answer Verification Agent")
    parser.add_argument("--input", required=True, help="Verification input JSON path")
    parser.add_argument("--output", required=True, help="Verification output JSON path")
    parser.add_argument("--report-output", help="Report JSON path")
    parser.add_argument("--qca-output", help="QCA JSON path")
    parser.add_argument("--failed-output", help="Failed items JSON path")
    parser.add_argument(
        "--provider",
        choices=("fake", "openai"),
        default="fake",
        help="Evaluator provider mode",
    )
    parser.add_argument("--model", default="configurable", help="Evaluator model name")
    parser.add_argument("--timeout", type=int, default=30, help="Request timeout seconds")
    parser.add_argument("--max-retries", type=int, default=2, help="Max retry count")
    parser.add_argument(
        "--evaluate-suspicious-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Evaluate only suspicious sentences",
    )
    parser.add_argument(
        "--all-sentences",
        action="store_true",
        help="Evaluate every parsed sentence",
    )
    parser.add_argument("--pretty", action="store_true", help="Print compact summary")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    args = build_parser().parse_args(argv)
    config = AnswerVerificationConfig(
        evaluator_model=args.model,
        timeout_seconds=args.timeout,
        max_retries=args.max_retries,
    )
    output_path = Path(args.output)
    result = run_verification_workflow(
        input_path=Path(args.input),
        output_path=output_path,
        report_output_path=Path(args.report_output)
        if args.report_output
        else output_path.with_name("verification_report.json"),
        qca_output_path=Path(args.qca_output)
        if args.qca_output
        else output_path.with_name("qca_output.json"),
        failed_output_path=Path(args.failed_output)
        if args.failed_output
        else output_path.with_name("failed_items.json"),
        provider_mode=args.provider,
        config=config,
        evaluate_suspicious_only=(
            False if args.all_sentences else args.evaluate_suspicious_only
        ),
    )
    print(f"validated status={result.status} execution_mode={result.execution_mode}")
    return 0 if result.status in {"success", "partial_success"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
