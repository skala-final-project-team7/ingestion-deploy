"""app.ingestion.extractor — 첨부 파일 텍스트 추출기 (FR-002).

raw_attachments 의 PDF/Word/Excel 바이너리에서 텍스트를 추출(이미지·도형 제외)해
``raw_attachments.extracted_text`` 에 보존한다(별도 attachment_texts 컬렉션 미사용 —
db-schema §2.7). Excel/CSV 는 시트를 자연어로 직렬화해 LLM 이 수치 맥락을 이해하도록 가공한다.
추출 1차/폴백 라이브러리는 pyproject `[ingestion]` extras(pymupdf/pdfplumber/python-docx/
openpyxl/pandas)를 따른다.
"""

from app.ingestion.extractor.base import ExtractionResult, extract_attachment_text

__all__ = ["ExtractionResult", "extract_attachment_text"]
