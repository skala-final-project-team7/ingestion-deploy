"""첨부 텍스트 추출 인터페이스 (FR-002) [Pipeline].

--------------------------------------------------
작성자 : 최태성
작성목적 : 첨부 바이너리(PDF/Word/Excel/CSV)를 텍스트로 추출하는 결정론 Pipeline 의 진입점.
          유형별 추출기(pdf/docx/spreadsheet)로 디스패치하고, 추출 실패는 쿼리 전체 실패로
          전파하지 않고 ``ok=False``/``reason`` 으로 격리한다(graceful degrade). 이미지·도형은
          제외하고 텍스트만 추출한다(FR-002).
          NOTE: 운영 첨부 청킹 경로는 파일 기반 ``chunk_attachment``(다운로더가 채운
          ``local_path`` 직접 읽기)다 — 본 모듈은 **bytes 입력용 추출 seam** 으로 유지된다
          (파일로 내려받지 않는 공급원·스트림 추출용. 현재 프로덕션 배선 없음).
작성일 : 2026-05-26 (stub → featureI-3 구현)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-26, featureI-3, 유형별 추출 디스패치 + graceful degrade 구현.
  - 2026-06-10, 배포 전 점검 — attachment_texts 잔존 참조 제거, 운영 경로(파일 기반
    chunk_attachment)와의 관계 명시(bytes 기반 보조 seam).
--------------------------------------------------
[보안] reason 에는 예외 타입명만 남기고 첨부 내용·자격증명을 포함하지 않는다.
--------------------------------------------------
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.enums import AttachmentType, ExtractedFormat

_SHEET_TYPES = (AttachmentType.XLSX, AttachmentType.CSV)


@dataclass
class ExtractionResult:
    """첨부 1건의 텍스트 추출 결과."""

    attachment_id: str
    attachment_type: AttachmentType
    extracted_format: ExtractedFormat
    text: str
    # 추출 실패 시 False + reason (쿼리 전체 실패로 전파하지 않고 graceful degrade).
    ok: bool = True
    reason: str | None = None


def extract_attachment_text(
    *, attachment_id: str, attachment_type: AttachmentType, content: bytes
) -> ExtractionResult:
    """첨부 바이너리 → 텍스트 추출 (이미지·도형 제외).

    - PDF: PyMuPDF(fitz) 1차 → pdfplumber 폴백 (``RAW_TEXT``)
    - Word(docx): python-docx 본문/표 (``RAW_TEXT``)
    - Excel(xlsx)/CSV: openpyxl/csv → 시트 자연어 직렬화 (``SHEET_SERIALIZED``)

    Args:
        attachment_id: 첨부 식별자(결과에 그대로 보존).
        attachment_type: 첨부 유형(추출 전략 분기).
        content: 첨부 바이너리.

    Returns:
        ``ExtractionResult`` — 성공 시 ``ok=True`` + text, 실패 시 ``ok=False`` + reason(예외 타입).
    """
    extracted_format = (
        ExtractedFormat.SHEET_SERIALIZED
        if attachment_type in _SHEET_TYPES
        else ExtractedFormat.RAW_TEXT
    )
    try:
        text = _dispatch(attachment_type, content)
    except Exception as exc:  # noqa: BLE001 — 추출 실패 격리(graceful degrade)
        return ExtractionResult(
            attachment_id=attachment_id,
            attachment_type=attachment_type,
            extracted_format=extracted_format,
            text="",
            ok=False,
            reason=type(exc).__name__,
        )
    return ExtractionResult(
        attachment_id=attachment_id,
        attachment_type=attachment_type,
        extracted_format=extracted_format,
        text=text,
        ok=True,
    )


def _dispatch(attachment_type: AttachmentType, content: bytes) -> str:
    """유형별 추출기로 디스패치한다(라이브러리는 각 모듈에서 지연 import)."""
    if attachment_type is AttachmentType.PDF:
        from app.ingestion.extractor.pdf import extract_pdf_text

        return extract_pdf_text(content)
    if attachment_type is AttachmentType.DOCX:
        from app.ingestion.extractor.docx import extract_docx_text

        return extract_docx_text(content)
    if attachment_type is AttachmentType.XLSX:
        from app.ingestion.extractor.spreadsheet import extract_xlsx_text

        return extract_xlsx_text(content)
    if attachment_type is AttachmentType.CSV:
        from app.ingestion.extractor.spreadsheet import extract_csv_text

        return extract_csv_text(content)
    raise ValueError(f"unsupported attachment_type: {attachment_type!r}")
