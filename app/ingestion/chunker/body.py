"""본문 6유형 1차 분할 + 페이지 청킹 엔트리.

--------------------------------------------------
작성자 : 최태성
담당 영역 : ingestion
작성목적 : Confluence 본문(body_html)을 doc_type별 논리 단위로 1차 분할하고,
          크기 규칙·메타데이터 부착을 거쳐 Chunk 목록을 산출한다 (chunking-strategy.md §3·§4).
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature3-B — 본문 6유형 분할기 + split_body + chunk_page
--------------------------------------------------
[호환성]
  - Python 3.11.x, Pydantic 2.7+, beautifulsoup4 4.12+
--------------------------------------------------
"""

import re

from app.ingestion.chunker.base import ChunkDraft, apply_size_rules
from app.ingestion.chunker.metadata import build_metadata
from app.ingestion.chunker.storage_format import clean_storage_format
from app.schemas.chunk import Chunk
from app.schemas.enums import DocType
from app.schemas.page_object import PageObject

_ANY_HEADING = re.compile(r"^#{2,4}\s+(.+)$", re.MULTILINE)

# 라벨 → doc_type PoC 추정 매핑. 실제 doc_type은 문서 분석기 Agent(feature6)가 결정한다.
_LABEL_TO_DOC_TYPE = {
    "장애대응": DocType.INCIDENT,
    "troubleshooting": DocType.TROUBLESHOOT,
    "faq": DocType.FAQ,
    "회의록": DocType.MEETING,
    "adr": DocType.ADR,
}


def _coerce_doc_type(doc_type: DocType | str) -> DocType:
    """doc_type을 DocType으로 변환한다. 미인식 값은 operation 기본값(chunking-strategy.md §6.3)."""
    if isinstance(doc_type, DocType):
        return doc_type
    try:
        return DocType(doc_type)
    except ValueError:
        return DocType.OPERATION


def _split_sections(text: str, level: int) -> tuple[str, list[tuple[str, str]]]:
    """text를 '#'*level 헤딩 기준으로 (preamble, [(header, body), ...])로 분할한다."""
    prefix = "#" * level + " "
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        if line.startswith(prefix):
            sections.append((line[len(prefix) :].strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return (
        "\n".join(preamble).strip(),
        [(header, "\n".join(body).strip()) for header, body in sections],
    )


def _first_heading(text: str) -> str | None:
    match = _ANY_HEADING.search(text)
    return match.group(1).strip() if match else None


def _sections_to_drafts(
    preamble: str,
    sections: list[tuple[str, str]],
    *,
    is_atomic: bool,
) -> list[ChunkDraft]:
    """(header, body) 섹션 목록을 ChunkDraft로 변환한다. preamble은 첫 섹션에 부착한다."""
    drafts: list[ChunkDraft] = []
    for index, (header, body) in enumerate(sections):
        text = f"{header}\n{body}".strip() if body else header
        if index == 0 and preamble:
            text = f"{preamble}\n\n{text}"
        drafts.append(
            ChunkDraft(text=text, section_header=header or "untitled", is_atomic=is_atomic)
        )
    return drafts


def _split_by_heading_levels(
    text: str,
    levels: tuple[int, ...],
    *,
    is_atomic: bool,
) -> list[ChunkDraft] | None:
    """levels 순서대로 헤딩 분할을 시도한다. 섹션이 1개 이상이면 ChunkDraft 목록을 반환한다."""
    for level in levels:
        preamble, sections = _split_sections(text, level)
        if sections:
            return _sections_to_drafts(preamble, sections, is_atomic=is_atomic)
    return None


def _split_operation(text: str) -> list[ChunkDraft]:
    """운영 매뉴얼 — H2(없으면 H3) 섹션 단위. 원자성 없음."""
    drafts = _split_by_heading_levels(text, (2, 3), is_atomic=False)
    return drafts or [ChunkDraft(text=text, section_header="untitled", is_atomic=False)]


def _split_incident(text: str) -> list[ChunkDraft]:
    """장애 대응 — 타임라인/원인/해결/재발방지 블록(H2/H3) 단위. 각 블록 원자성."""
    drafts = _split_by_heading_levels(text, (2, 3), is_atomic=True)
    return drafts or [ChunkDraft(text=text, section_header="untitled", is_atomic=True)]


def _split_troubleshoot(text: str) -> list[ChunkDraft]:
    """트러블슈팅 — 증상-원인-해결 케이스(H3 우선, 없으면 H2) 단위. 각 케이스 원자성."""
    h3_preamble, h3_sections = _split_sections(text, 3)
    if len(h3_sections) >= 2:
        return _sections_to_drafts(h3_preamble, h3_sections, is_atomic=True)
    drafts = _split_by_heading_levels(text, (2, 3), is_atomic=True)
    return drafts or [ChunkDraft(text=text, section_header="untitled", is_atomic=True)]


def _split_adr(text: str) -> list[ChunkDraft]:
    """ADR — 전체를 단일 청크로 유지(원자성 최우선)."""
    return [ChunkDraft(text=text, section_header=_first_heading(text) or "ADR", is_atomic=True)]


def _split_by_question_lines(text: str) -> list[ChunkDraft]:
    """'?'로 끝나는 줄을 질문 경계로 보고 Q&A 쌍으로 분할한다. 2쌍 미만이면 빈 목록."""
    pairs: list[list[str]] = []
    for line in text.split("\n"):
        if line.strip().endswith("?"):
            pairs.append([line])
        elif pairs:
            pairs[-1].append(line)
    if len(pairs) < 2:
        return []
    return [
        ChunkDraft(
            text="\n".join(pair).strip(),
            section_header=pair[0].strip().lstrip("#").strip() or "FAQ",
            is_atomic=True,
        )
        for pair in pairs
    ]


def _split_faq(text: str) -> list[ChunkDraft]:
    """FAQ — Q&A 쌍 단위(H3/H4 헤딩 우선, 없으면 '?' 줄 기준). 각 쌍 원자성, 분리 금지."""
    drafts = _split_by_heading_levels(text, (3, 4), is_atomic=True)
    if drafts and len(drafts) >= 2:
        return drafts
    by_question = _split_by_question_lines(text)
    if by_question:
        return by_question
    return [ChunkDraft(text=text, section_header=_first_heading(text) or "FAQ", is_atomic=True)]


def _split_meeting(text: str) -> list[ChunkDraft]:
    """회의록 — 안건(H2/H3) 단위. 상단 메타(일자·참석자)를 각 안건 도입부에 부착. 각 안건 원자성."""
    for level in (2, 3):
        preamble, sections = _split_sections(text, level)
        if sections:
            drafts: list[ChunkDraft] = []
            for header, body in sections:
                agenda = f"{header}\n{body}".strip() if body else header
                combined = f"{preamble}\n\n{agenda}".strip() if preamble else agenda
                drafts.append(
                    ChunkDraft(text=combined, section_header=header or "untitled", is_atomic=True)
                )
            return drafts
    return [ChunkDraft(text=text, section_header=_first_heading(text) or "회의록", is_atomic=True)]


_SPLITTERS = {
    DocType.OPERATION: _split_operation,
    DocType.INCIDENT: _split_incident,
    DocType.TROUBLESHOOT: _split_troubleshoot,
    DocType.ADR: _split_adr,
    DocType.FAQ: _split_faq,
    DocType.MEETING: _split_meeting,
}


def split_body(body_html: str, doc_type: DocType | str) -> list[ChunkDraft]:
    """본문 HTML을 정제 후 doc_type별 전략으로 1차 분할한다.

    2단계 크기 규칙(2차 재분할·하한선 병합)은 적용하지 않는다 — chunk_page에서 적용한다.

    Args:
        body_html: PageObject.body_html (Confluence Storage Format).
        doc_type: 본문 문서 유형. 미인식 값은 operation으로 폴백.

    Returns:
        1차 분할 결과 ChunkDraft 목록. 본문이 비면 빈 목록.
    """
    cleaned = clean_storage_format(body_html)
    if not cleaned:
        return []
    splitter = _SPLITTERS[_coerce_doc_type(doc_type)]
    return [draft for draft in splitter(cleaned) if draft.text.strip()]


def infer_doc_type(page: PageObject) -> DocType:
    """라벨 기반 doc_type 추정 — PoC 휴리스틱.

    실제 doc_type은 문서 분석기 Agent(feature6)가 결정한다. 본 함수는 feature3 단독
    테스트·데모를 위한 임시 추정기이며, 매칭 라벨이 없으면 operation을 반환한다.
    """
    labels = {label.lower() for label in page.labels}
    for label, doc_type in _LABEL_TO_DOC_TYPE.items():
        if label.lower() in labels:
            return doc_type
    return DocType.OPERATION


def chunk_page(page: PageObject, doc_type: DocType | str | None = None) -> list[Chunk]:
    """PageObject 본문을 doc_type별 전략으로 분할해 Chunk 목록을 반환한다.

    1차 분할 → 2단계 크기 규칙(2차 재분할·하한선 병합) → 메타데이터 부착 순으로 처리한다.
    doc_type이 None이면 라벨 기반 PoC 휴리스틱(infer_doc_type)으로 추정한다.
    """
    resolved = _coerce_doc_type(doc_type) if doc_type is not None else infer_doc_type(page)
    drafts = apply_size_rules(split_body(page.body_html, resolved))
    return [
        Chunk(text=draft.text, metadata=build_metadata(page, draft, index, resolved))
        for index, draft in enumerate(drafts)
        if draft.text.strip()
    ]
