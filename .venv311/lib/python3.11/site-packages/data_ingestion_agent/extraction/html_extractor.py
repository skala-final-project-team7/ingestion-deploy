from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Confluence storage HTML 원문 보존 및 plain text 추출 서비스 구현.
          HTML extraction은 후속 processed document pipeline과 분리된 일반 함수로 제공한다.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature3 HTML extractor 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 html.parser 기반
--------------------------------------------------
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser

BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "div",
    "dl",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "main",
    "nav",
    "p",
    "pre",
    "section",
}
LIST_CONTAINER_TAGS = {"ul", "ol"}
LIST_ITEM_TAG = "li"
TABLE_ROW_TAG = "tr"
TABLE_CELL_TAGS = {"td", "th"}
IGNORED_CONTENT_TAGS = {"script", "style"}
WHITESPACE_PATTERN = re.compile(r"[ \r\f\v]+")


@dataclass(frozen=True, slots=True)
class HtmlExtractionResult:
    """Storage HTML 원문과 추출된 plain text를 함께 반환하는 결과."""

    storage_html: str
    plain_text: str


def extract_storage_html(storage_html: str | None) -> HtmlExtractionResult:
    """Confluence storage HTML을 원문 보존 결과와 plain text로 변환한다.

    Args:
        storage_html: Confluence Page 상세의 `body.storage.value` 문자열.

    Returns:
        원문 storage HTML과 정규화된 plain text를 분리한 결과.
    """
    original_html = storage_html or ""
    if not original_html:
        return HtmlExtractionResult(storage_html="", plain_text="")

    parser = _StorageHtmlPlainTextParser()
    parser.feed(original_html)
    parser.close()
    return HtmlExtractionResult(
        storage_html=original_html,
        plain_text=_normalize_plain_text(parser.plain_text()),
    )


class _StorageHtmlPlainTextParser(HTMLParser):
    """Confluence storage HTML에서 읽을 수 있는 본문 텍스트만 수집한다."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_depth = 0
        self._list_stack: list[str] = []
        self._ordered_item_counts: list[int] = []
        self._is_in_table_row = False
        self._is_in_table_cell = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        normalized_tag = tag.lower()

        if normalized_tag in IGNORED_CONTENT_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return

        if normalized_tag in BLOCK_TAGS:
            self._append_line_break()
        elif normalized_tag in LIST_CONTAINER_TAGS:
            self._list_stack.append(normalized_tag)
            if normalized_tag == "ol":
                self._ordered_item_counts.append(0)
            self._append_line_break()
        elif normalized_tag == LIST_ITEM_TAG:
            self._append_line_break()
            self._append_list_marker()
        elif normalized_tag == TABLE_ROW_TAG:
            self._is_in_table_row = True
            self._append_line_break()
        elif normalized_tag in TABLE_CELL_TAGS:
            if self._is_in_table_cell:
                self._append_text(" ")
            elif self._is_in_table_row and self._current_line_has_text():
                self._append_text("\t")
            self._is_in_table_cell = True

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()

        if normalized_tag in IGNORED_CONTENT_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return

        if normalized_tag in BLOCK_TAGS:
            self._append_line_break()
        elif normalized_tag in LIST_CONTAINER_TAGS:
            if self._list_stack:
                ended_tag = self._list_stack.pop()
                if ended_tag == "ol" and self._ordered_item_counts:
                    self._ordered_item_counts.pop()
            self._append_line_break()
        elif normalized_tag == LIST_ITEM_TAG:
            self._append_line_break()
        elif normalized_tag == TABLE_ROW_TAG:
            self._is_in_table_row = False
            self._append_line_break()
        elif normalized_tag in TABLE_CELL_TAGS:
            self._is_in_table_cell = False

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        self._append_text(data)

    def plain_text(self) -> str:
        """수집한 parser token을 하나의 plain text 문자열로 반환한다."""
        return "".join(self._parts)

    def _append_list_marker(self) -> None:
        if self._list_stack and self._list_stack[-1] == "ol":
            self._ordered_item_counts[-1] += 1
            self._append_text(f"{self._ordered_item_counts[-1]}. ")
        else:
            self._append_text("- ")

    def _append_text(self, text: str) -> None:
        if not text:
            return
        self._parts.append(text.replace("\xa0", " "))

    def _append_line_break(self) -> None:
        if not self._parts or self._parts[-1].endswith("\n"):
            return
        self._parts.append("\n")

    def _current_line_has_text(self) -> bool:
        current_line_parts: list[str] = []
        for part in reversed(self._parts):
            if "\n" in part:
                current_line_parts.append(part.rsplit("\n", maxsplit=1)[-1])
                break
            current_line_parts.append(part)
        return bool("".join(reversed(current_line_parts)).strip())


def _normalize_plain_text(text: str) -> str:
    normalized_lines: list[str] = []
    for line in text.splitlines():
        normalized_line = WHITESPACE_PATTERN.sub(" ", line).strip()
        if normalized_line:
            normalized_lines.append(normalized_line)
    return "\n".join(normalized_lines)
