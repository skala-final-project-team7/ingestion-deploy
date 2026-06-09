"""PDF 텍스트 추출 (FR-002) — PyMuPDF(fitz) 1차 → pdfplumber 폴백 [Pipeline].

이미지·도형은 추출하지 않고 텍스트 레이어만 추출한다. 라이브러리는 지연 import 하여
app import 가 ingestion extras 미설치 환경에서도 동작하도록 한다.
"""

from __future__ import annotations

import io


def extract_pdf_text(content: bytes) -> str:
    """PDF 바이너리에서 텍스트를 추출한다.

    PyMuPDF(fitz)로 1차 추출하고, 예외 또는 빈 결과면 pdfplumber 로 폴백한다(복잡 레이아웃).
    둘 다 텍스트가 없으면 빈 문자열을 반환한다(스캔 PDF 등 — 호출자가 품질 판정).
    """
    try:
        text = _extract_with_pymupdf(content)
    except Exception:
        text = ""
    if text.strip():
        return text
    return _extract_with_pdfplumber(content)


def _extract_with_pymupdf(content: bytes) -> str:
    import fitz  # PyMuPDF

    parts: list[str] = []
    with fitz.open(stream=content, filetype="pdf") as document:
        for page in document:
            parts.append(page.get_text("text"))
    return "\n".join(parts).strip()


def _extract_with_pdfplumber(content: bytes) -> str:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts).strip()
