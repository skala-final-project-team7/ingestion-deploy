"""Confluence Storage Format(HTML) 본문 공통 전처리.

--------------------------------------------------
작성자 : 최태성
작성목적 : doc_type별 1차 분할 파서가 깨끗한 입력을 받도록, Confluence Storage Format
          본문을 정규화된 텍스트로 변환한다. 헤딩은 markdown(##/###/####), 코드는
          ``` 펜스/백틱, 표는 markdown 표, code 매크로·task-list는 텍스트로 변환한다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature3-A — clean_storage_format (samples 본문 패턴 기준)
  - 2026-05-17, 코드 리뷰 후속(P2) — Hugo 숏코드(`{{< ... >}}`) 잔재를 정제 단계에서
    제거해 datadog 본문 임베딩 잡음 감소
--------------------------------------------------
[호환성]
  - Python 3.11.x, beautifulsoup4 4.12+
  - NOTE: samples/*.json 본문은 code 매크로·task-list 외 다른 ac:* 매크로·이미지가
          없어 그 두 가지만 변환한다. 기타 ac:*/ri:* 태그는 제거한다.
--------------------------------------------------
"""

import html as html_lib
import re
from itertools import count

from bs4 import BeautifulSoup, NavigableString, Tag

_HEADING_PREFIX = {
    "h1": "#",
    "h2": "##",
    "h3": "###",
    "h4": "####",
    "h5": "#####",
    "h6": "######",
}
_SMART_QUOTES = {"“": '"', "”": '"', "‘": "'", "’": "'"}
_PLACEHOLDER = "__CHUNKER_CODE_PLACEHOLDER_{index}__"

# code 매크로: <ac:structured-macro ac:name="code"> ... <ac:plain-text-body>CODE</...> ...
_CODE_MACRO = re.compile(
    r'<ac:structured-macro\s+ac:name="code".*?'
    r"<ac:plain-text-body>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</ac:plain-text-body>"
    r".*?</ac:structured-macro>",
    re.DOTALL,
)
_INLINE_CODE = re.compile(r"<code>(.*?)</code>", re.DOTALL)
_TASK = re.compile(
    r"<ac:task>\s*<ac:task-status>(.*?)</ac:task-status>\s*"
    r"<ac:task-body>(.*?)</ac:task-body>\s*</ac:task>",
    re.DOTALL,
)
_CONFLUENCE_TAG = re.compile(r"</?(?:ac|ri):[^>]*>")
# Hugo 숏코드 잔재(datadog 본문 등) — `{{< ref "..." >}}` 같은 형태를 통째로 제거한다.
_HUGO_SHORTCODE = re.compile(r"\{\{[<%].*?[>%]\}\}", re.DOTALL)


def clean_storage_format(html: str) -> str:
    """Confluence Storage Format(HTML) 본문을 정규화된 텍스트로 변환한다.

    Args:
        html: PageObject.body_html (Confluence Storage Format).

    Returns:
        정규화된 텍스트. 헤딩은 markdown, 코드는 펜스/백틱, 표는 markdown 표.
        파싱 실패 시 태그를 제거한 plain text로 폴백한다.
    """
    if not html or not html.strip():
        return ""
    try:
        protected, code_blocks = _protect_code(html)
        protected = _TASK.sub(_render_task, protected)
        protected = _CONFLUENCE_TAG.sub("", protected)
        soup = BeautifulSoup(protected, "html.parser")
        text = _render_children(soup)
        for placeholder, snippet in code_blocks.items():
            text = text.replace(placeholder, snippet)
        return _normalize(text)
    except Exception:
        return _plain_text_fallback(html)


def _protect_code(markup: str) -> tuple[str, dict[str, str]]:
    """code 매크로·인라인 <code>를 플레이스홀더로 치환해 HTML 파서로부터 보호한다.

    코드 내부의 '<env>' 같은 텍스트가 태그로 파싱되어 사라지는 것을 방지한다.
    """
    blocks: dict[str, str] = {}
    counter = count()

    def _block(match: re.Match[str]) -> str:
        key = _PLACEHOLDER.format(index=next(counter))
        code = html_lib.unescape(match.group(1)).strip()
        blocks[key] = f"\n```\n{code}\n```\n"
        return key

    def _inline(match: re.Match[str]) -> str:
        key = _PLACEHOLDER.format(index=next(counter))
        code = html_lib.unescape(match.group(1)).strip()
        blocks[key] = f"`{code}`"
        return key

    markup = _CODE_MACRO.sub(_block, markup)
    markup = _INLINE_CODE.sub(_inline, markup)
    return markup, blocks


def _render_task(match: re.Match[str]) -> str:
    """ac:task → markdown 체크박스 텍스트."""
    status = match.group(1).strip()
    body = match.group(2).strip()
    mark = "x" if status == "complete" else " "
    return f"\n- [{mark}] {body}"


def _render_children(node: Tag) -> str:
    return "".join(_render(child) for child in node.children)


def _render(node: object) -> str:
    """BeautifulSoup 노드를 정규화 텍스트로 재귀 렌더링한다."""
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    name = node.name
    if name in _HEADING_PREFIX:
        return f"\n{_HEADING_PREFIX[name]} {_render_children(node).strip()}\n"
    if name == "p":
        return _render_children(node).strip() + "\n"
    if name == "br":
        return "\n"
    if name == "table":
        return "\n" + _table_to_markdown(node) + "\n"
    if name in ("ul", "ol"):
        return "\n" + _list_to_markdown(node) + "\n"
    if name == "li":
        return _render_children(node).strip()
    # strong/em/u/a/span 등 인라인·기타 태그는 자식 텍스트만 유지한다.
    return _render_children(node)


def _list_to_markdown(node: Tag) -> str:
    ordered = node.name == "ol"
    lines: list[str] = []
    for index, item in enumerate(node.find_all("li", recursive=False), start=1):
        prefix = f"{index}. " if ordered else "- "
        lines.append(prefix + _render_children(item).strip())
    return "\n".join(lines)


def _table_to_markdown(node: Tag) -> str:
    rows = node.find_all("tr")
    if not rows:
        return ""
    markdown_rows: list[str] = []
    for row_index, row in enumerate(rows):
        cells = [
            _render_children(cell).strip().replace("\n", " ")
            for cell in row.find_all(["th", "td"], recursive=False)
        ]
        markdown_rows.append("| " + " | ".join(cells) + " |")
        if row_index == 0:
            markdown_rows.append("| " + " | ".join("---" for _ in cells) + " |")
    return "\n".join(markdown_rows)


def _normalize(text: str) -> str:
    """스마트 따옴표를 ASCII로, Hugo 숏코드를 제거하고, 과도한 공백·빈 줄을 정리한다."""
    for smart, ascii_char in _SMART_QUOTES.items():
        text = text.replace(smart, ascii_char)
    text = _HUGO_SHORTCODE.sub("", text)
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _plain_text_fallback(html: str) -> str:
    """파싱 실패 시 태그만 제거한 plain text를 반환한다."""
    text = re.sub(r"<[^>]+>", " ", html)
    return _normalize(html_lib.unescape(text))
