from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Ingestion Agent local JSON/JSONL output repository 구현.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature4 local file repository 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 pathlib/json 기반
--------------------------------------------------
"""

import json
from dataclasses import dataclass
from pathlib import Path

from data_ingestion_agent.schemas import FailedItem, IngestionReport, ProcessedDocument


@dataclass(frozen=True, slots=True)
class LocalWriteResult:
    """Local file repository output path 결과."""

    documents_path: Path
    failed_items_path: Path
    report_path: Path
    output_paths: dict[str, str]


class LocalFileRepository:
    """MVP local file output repository.

    MongoDB repository adapter로 교체할 수 있도록 documents, failed items, report를
    schema 객체 단위로 받아 JSON/JSONL serialization만 담당한다.
    """

    def __init__(self, output_root: Path | str) -> None:
        self.output_root = Path(output_root)

    def write_outputs(
        self,
        *,
        documents: list[ProcessedDocument],
        failed_items: list[FailedItem],
        report: IngestionReport,
    ) -> LocalWriteResult:
        """Processed documents, failed items, report를 local file로 저장한다."""
        processed_dir = self.output_root / "processed"
        failed_dir = self.output_root / "failed"
        reports_dir = self.output_root / "reports"
        for output_dir in (processed_dir, failed_dir, reports_dir):
            output_dir.mkdir(parents=True, exist_ok=True)

        documents_path = processed_dir / "documents.jsonl"
        failed_items_path = failed_dir / "failed_items.jsonl"
        report_path = reports_dir / "ingestion_report.json"

        self._write_jsonl(documents_path, [document.to_dict() for document in documents])
        self._write_jsonl(
            failed_items_path,
            [failed_item.to_dict() for failed_item in failed_items],
        )

        output_paths = {
            "documents": str(documents_path),
            "failed_items": str(failed_items_path),
            "report": str(report_path),
        }
        report.output_paths.update(output_paths)
        self._write_json(report_path, report.to_dict())

        return LocalWriteResult(
            documents_path=documents_path,
            failed_items_path=failed_items_path,
            report_path=report_path,
            output_paths=output_paths,
        )

    @staticmethod
    def _write_jsonl(path: Path, rows: list[dict]) -> None:
        with path.open("w", encoding="utf-8") as output_file:
            for row in rows:
                output_file.write(
                    json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                )

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
