"""첨부 텍스트 추출기 단위 테스트 (FR-002) — 유형별 추출 + graceful degrade.

작성자 : 최태성
담당 영역 : ingestion

샘플 첨부는 in-test 로 생성한다(python-docx/openpyxl/PyMuPDF). 추출 라이브러리는 ingestion
extras 에 포함되며, 미설치 환경에서는 해당 테스트를 skip 한다(코어 디스패치·실패 격리는 항상 검증).
"""

from __future__ import annotations

import io

import pytest

from app.ingestion.extractor import ExtractionResult, extract_attachment_text
from app.schemas.enums import AttachmentType, ExtractedFormat


def _extract(attachment_type: AttachmentType, content: bytes) -> ExtractionResult:
    return extract_attachment_text(
        attachment_id="att-1", attachment_type=attachment_type, content=content
    )


def test_docx_extracts_paragraphs_and_tables() -> None:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("Restart the service")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Step"
    table.rows[0].cells[1].text = "Clear cache"
    buffer = io.BytesIO()
    document.save(buffer)

    result = _extract(AttachmentType.DOCX, buffer.getvalue())

    assert result.ok is True
    assert result.attachment_id == "att-1"
    assert result.extracted_format is ExtractedFormat.RAW_TEXT
    assert "Restart the service" in result.text
    assert "Step | Clear cache" in result.text


def test_xlsx_serializes_sheets_as_natural_language() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Metrics"
    sheet.append(["region", "sales"])
    sheet.append(["KR", 100])
    buffer = io.BytesIO()
    workbook.save(buffer)

    result = _extract(AttachmentType.XLSX, buffer.getvalue())

    assert result.ok is True
    assert result.extracted_format is ExtractedFormat.SHEET_SERIALIZED
    assert "Sheet: Metrics" in result.text
    assert "region: KR, sales: 100" in result.text


def test_csv_serializes_rows_with_header_mapping() -> None:
    result = _extract(AttachmentType.CSV, b"region,sales\nKR,100\nUS,200\n")

    assert result.ok is True
    assert result.extracted_format is ExtractedFormat.SHEET_SERIALIZED
    assert "Sheet: CSV" in result.text
    assert "region: KR, sales: 100" in result.text
    assert "region: US, sales: 200" in result.text


def test_pdf_extracts_text_layer() -> None:
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Runbook restart procedure")
    content = document.tobytes()
    document.close()

    result = _extract(AttachmentType.PDF, content)

    assert result.ok is True
    assert result.extracted_format is ExtractedFormat.RAW_TEXT
    assert "Runbook restart procedure" in result.text


def test_corrupt_binary_degrades_gracefully() -> None:
    pytest.importorskip("fitz")
    result = _extract(AttachmentType.PDF, b"not a real pdf")

    assert result.ok is False
    assert result.text == ""
    assert result.reason  # 예외 타입명(내용·자격증명 미포함)
    # 첨부 메타는 실패해도 보존된다(쿼리 전체 실패로 전파하지 않음).
    assert result.attachment_id == "att-1"
    assert result.attachment_type is AttachmentType.PDF


def test_empty_csv_yields_empty_text_but_ok() -> None:
    result = _extract(AttachmentType.CSV, b"")

    assert result.ok is True
    assert result.text == ""
