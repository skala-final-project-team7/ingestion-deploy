from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Sync Agent storage HTML plain text extraction 구현.
          Confluence storage HTML 원문을 보존하면서 후속 changed document용 plain text를 만든다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature5 HTML extraction 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 html.parser 기반
--------------------------------------------------
"""

from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass(frozen=True, slots=True)
class HtmlExtractionResult:
    """storage HTML 원문과 추출된 plain text."""

    storage_html: str
    plain_text: str
    has_unsupported_content: bool


def extract_storage_html(storage_html: str | None) -> HtmlExtractionResult:
    """Confluence storage HTML을 보존하고 읽을 수 있는 plain text를 추출한다."""
    html = storage_html or ""
    parser = _PlainTextParser()
    parser.feed(html)
    parser.close()
    plain_text = _normalize_text(parser.text())
    return HtmlExtractionResult(
        storage_html=html,
        plain_text=plain_text,
        has_unsupported_content=parser.has_unsupported_content,
    )


class _PlainTextParser(HTMLParser):
    _BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
    _IGNORED_TAGS = {"script", "style"}
    _UNSUPPORTED_PREFIXES = ("ac:", "ri:")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_depth = 0
        self.has_unsupported_content = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if tag.startswith(self._UNSUPPORTED_PREFIXES):
            self.has_unsupported_content = True
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        # HTMLParser(convert_charrefs=True) 가 handle_data 호출 전에 이미 모든 문자 참조를
        # 디코딩한다. 여기서 unescape 를 또 호출하면 이중 디코딩되어, 화면에 리터럴로
        # 표시하려고 이중 인코딩한 콘텐츠(예: storage HTML 의 ``&amp;lt;`` = 표시상 ``&lt;``)가
        # ``<`` 로 손상된다. 따라서 추가 unescape 없이 data 를 그대로 사용한다.
        if data:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _normalize_text(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    compact_lines = [line for line in lines if line]
    return "\n".join(compact_lines)
