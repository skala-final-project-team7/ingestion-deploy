"""청킹 2단계 하이브리드 분할 공통 로직.

--------------------------------------------------
작성자 : 최태성
작성목적 : 1차 분할(doc_type별 논리 단위 — body.py 담당) 결과에 적용하는
          2차 재분할(800토큰 초과 → 100토큰 오버랩)과 하한선 병합(200토큰 미만)을
          제공한다. 원자성 유지 유형은 두 단계 모두 제외된다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature3-A — ChunkDraft / split_oversized / merge_undersized
  - 2026-05-15, 하한선 병합 붕괴 버그 수정, merge_undersized가 하한선을 채운 직전 청크를
    '봉인'하도록 변경 — 작은 청크가 한 청크로 무한 누적되던 문제 해결 (feature4-A 중 발견)
  - 2026-06-10, 코드 리뷰 재점검(P2-9) — OVERSIZE_ATOMIC 강제 분할 구현. 원자성 청크도
    ATOMIC_MAX_TOKENS(1500) 초과 시 강제 분할하고 section_header 에 ``(Part N/M)`` 을
    표기한다(chunking-strategy.md §8 — 종전에는 전부 스킵되어 임베딩 입력 윈도를 초과).
--------------------------------------------------
[호환성]
  - Python 3.11.x
--------------------------------------------------
"""

from dataclasses import dataclass

from app.ingestion.chunker.tokenizer import count_tokens

# chunking-strategy.md §5.2 임계값
MAX_TOKENS = 800  # 2차 재분할 임계
OVERLAP_TOKENS = 100  # 2차 재분할 오버랩
MIN_TOKENS = 200  # 하한선 병합 임계
# chunking-strategy.md §8 — OVERSIZE_ATOMIC: 원자성 유형도 이 임계를 초과하면 강제 분할
# (+ ``Part N/M`` 표기). 임베딩 입력 윈도(e5 권장 512~1024) 초과 방지의 안전망이다.
ATOMIC_MAX_TOKENS = 1500


@dataclass
class ChunkDraft:
    """1차 분할 결과 단위. 2차 재분할·하한선 병합의 입출력 타입.

    Attributes:
        text: 청크 본문 텍스트.
        section_header: 출처 카드 섹션명 (H2/H3 또는 'p.N' 등).
        is_atomic: True이면 2차 재분할·하한선 병합에서 제외 (FAQ Q&A·ADR·회의록 안건 등).
    """

    text: str
    section_header: str
    is_atomic: bool = False


def _split_long_unit(unit: str, max_tokens: int) -> list[str]:
    """단일 단위(줄)가 max_tokens를 초과하면 단어 단위로 강제 분할한다."""
    if count_tokens(unit) <= max_tokens:
        return [unit]
    parts: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for word in unit.split():
        word_tokens = count_tokens(word)
        if current and current_tokens + word_tokens > max_tokens:
            parts.append(" ".join(current))
            current, current_tokens = [], 0
        current.append(word)
        current_tokens += word_tokens
    if current:
        parts.append(" ".join(current))
    return parts


def split_oversized(
    text: str,
    max_tokens: int = MAX_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[str]:
    """max_tokens 초과 텍스트를 overlap_tokens 오버랩 슬라이딩 윈도우로 재분할한다.

    임계 이하 텍스트는 그대로 단일 원소 리스트로 반환한다. 줄(\\n) 단위를 기본 단위로
    삼고, 한 줄이 임계를 초과하면 단어 단위로 강제 분할한 뒤 윈도잉한다.
    """
    if count_tokens(text) <= max_tokens:
        return [text]

    units: list[str] = []
    for line in text.split("\n"):
        if line.strip():
            units.extend(_split_long_unit(line, max_tokens))
    if not units:
        return [text]

    windows: list[str] = []
    start = 0
    total = len(units)
    while start < total:
        current: list[str] = []
        current_tokens = 0
        end = start
        while end < total:
            unit_tokens = count_tokens(units[end])
            if current and current_tokens + unit_tokens > max_tokens:
                break
            current.append(units[end])
            current_tokens += unit_tokens
            end += 1
        windows.append("\n".join(current))
        if end >= total:
            break
        # 오버랩: 다음 시작점을 overlap_tokens만큼 뒤로 당긴다 (최소 1단위 전진 보장).
        new_start = end
        overlap = 0
        while new_start > start + 1 and overlap < overlap_tokens:
            new_start -= 1
            overlap += count_tokens(units[new_start])
        start = new_start
    return windows


def merge_undersized(
    drafts: list[ChunkDraft],
    min_tokens: int = MIN_TOKENS,
) -> list[ChunkDraft]:
    """min_tokens 미만 청크를 직전 청크와 병합한다.

    원자성 청크(is_atomic)는 작아도 병합되지 않으며, 직전 청크가 원자성이거나
    없으면 작은 청크라도 그대로 유지한다.

    직전 청크가 이미 하한선(min_tokens)을 채웠으면 '봉인'되어 더 이상 병합 대상이
    되지 않는다. 이 봉인이 없으면 작은 청크가 직전 청크에 무한 누적되어 문서 전체가
    한 청크로 붕괴한다 (chunking-strategy.md §3 — 하한선 처리는 '직전/직후' 1회 병합 의도).
    """
    result: list[ChunkDraft] = []
    for draft in drafts:
        too_small = not draft.is_atomic and count_tokens(draft.text) < min_tokens
        can_merge = (
            bool(result) and not result[-1].is_atomic and count_tokens(result[-1].text) < min_tokens
        )
        if too_small and can_merge:
            previous = result[-1]
            result[-1] = ChunkDraft(
                text=f"{previous.text}\n{draft.text}",
                section_header=previous.section_header,
                is_atomic=False,
            )
        else:
            result.append(draft)
    return result


def apply_size_rules(drafts: list[ChunkDraft]) -> list[ChunkDraft]:
    """1차 분할 결과에 2차 재분할 → 하한선 병합을 순서대로 적용한다.

    원자성 유지 유형(FAQ Q&A·ADR·회의록 안건·트러블슈팅 케이스)은 원칙적으로 두 단계
    모두 제외되나, ``ATOMIC_MAX_TOKENS``(1500)를 초과하면 임베딩 입력 윈도 보호를 위해
    강제 분할하고 ``section_header`` 에 ``(Part N/M)`` 을 표기한다(OVERSIZE_ATOMIC —
    chunking-strategy.md §8, 코드 리뷰 P2-9). 분할된 파트는 원자성을 유지해 하한선
    병합 대상이 되지 않는다.
    """
    after_split: list[ChunkDraft] = []
    for draft in drafts:
        if draft.is_atomic:
            after_split.extend(_split_oversized_atomic(draft))
            continue
        for part in split_oversized(draft.text):
            after_split.append(
                ChunkDraft(text=part, section_header=draft.section_header, is_atomic=False)
            )
    return merge_undersized(after_split)


def _split_oversized_atomic(draft: ChunkDraft) -> list[ChunkDraft]:
    """OVERSIZE_ATOMIC 강제 분할 — 1500토큰 초과 원자성 청크를 Part N/M 으로 나눈다.

    임계 이하는 무수정 단일 원소로 반환한다(기존 동작 보존). 초과 시 본문 분할은
    ``split_oversized``(800토큰 윈도 + 100토큰 오버랩)를 재사용해 각 파트가 임베딩
    입력 윈도에 들어가게 한다.
    """
    if count_tokens(draft.text) <= ATOMIC_MAX_TOKENS:
        return [draft]
    parts = split_oversized(draft.text)
    total = len(parts)
    return [
        ChunkDraft(
            text=part,
            section_header=f"{draft.section_header} (Part {index}/{total})",
            is_atomic=True,
        )
        for index, part in enumerate(parts, start=1)
    ]
