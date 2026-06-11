from __future__ import annotations

"""Local JSON/JSONL output writer for Answer Verification Agent."""

import json
import re
from pathlib import Path
from typing import Any

from answer_verification_agent.verification.result_builder import (
    VerificationBuildResult,
)

_SENSITIVE_PATTERNS = [
    re.compile(r"OPENAI_API_KEY\s*=\s*[^,\s]+", re.IGNORECASE),
    re.compile(r"Authorization:\s*Bearer\s+[^,\s]+", re.IGNORECASE),
    re.compile(r"Bearer\s+[^,\s]+", re.IGNORECASE),
]


def write_verification_artifacts(
    result: VerificationBuildResult,
    output_dir: Path | str,
) -> dict[str, Path]:
    """Write verification output/report/QCA/failed local files."""
    base_dir = Path(output_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "output": base_dir / "verification_output.json",
        "report": base_dir / "verification_report.json",
        "qca_json": base_dir / "qca_output.json",
        "qca_jsonl": base_dir / "qca_output.jsonl",
        "failed": base_dir / "failed_items.json",
    }
    _write_json(paths["output"], result.output.to_dict())
    _write_json(paths["report"], result.report.to_dict())
    _write_json(paths["qca_json"], result.qca_output.to_dict())
    _write_jsonl(paths["qca_jsonl"], [result.qca_output.to_dict()])
    _write_json(paths["failed"], [item.to_dict() for item in result.failed_items])
    return paths


def write_verification_artifacts_to_paths(
    result: VerificationBuildResult,
    *,
    output_path: Path | str,
    report_output_path: Path | str,
    qca_output_path: Path | str,
    failed_output_path: Path | str,
) -> dict[str, Path]:
    """Write verification artifacts to explicit CLI paths."""
    paths = {
        "output": Path(output_path),
        "report": Path(report_output_path),
        "qca_json": Path(qca_output_path),
        "qca_jsonl": Path(qca_output_path).with_suffix(".jsonl"),
        "failed": Path(failed_output_path),
    }
    _write_json(paths["output"], result.output.to_dict())
    _write_json(paths["report"], result.report.to_dict())
    _write_json(paths["qca_json"], result.qca_output.to_dict())
    _write_jsonl(paths["qca_jsonl"], [result.qca_output.to_dict()])
    _write_json(paths["failed"], [item.to_dict() for item in result.failed_items])
    return paths


def write_failed_artifacts(
    *,
    output_path: Path | str,
    report_output_path: Path | str,
    failed_output_path: Path | str,
    failed_items: list[dict[str, Any]],
    report: dict[str, Any],
) -> dict[str, Path]:
    """Write safe failed output/report for unrecoverable workflow failures."""
    paths = {
        "output": Path(output_path),
        "report": Path(report_output_path),
        "failed": Path(failed_output_path),
    }
    _write_json(paths["output"], {"status": "failed", "failed_items": failed_items})
    _write_json(paths["report"], report)
    _write_json(paths["failed"], failed_items)
    return paths


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_redact_payload(payload), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _write_jsonl(path: Path, payloads: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(_redact_payload(payload), ensure_ascii=False, sort_keys=True)
        for payload in payloads
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _redact_payload(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_payload(item) for key, item in value.items()}
    return value


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub("<redacted>", redacted)
    return redacted
