"""청킹용 토큰 카운터.

--------------------------------------------------
작성자 : 최태성
담당 영역 : ingestion
작성목적 : Adaptive Chunker의 분할 임계(800/200 토큰) 판단을 위한 토큰 카운터.
          PoC 단계에서는 모델 로딩 없이 동작하는 휴리스틱을 사용한다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature3-A — PoC 휴리스틱 count_tokens
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - NOTE: 설계서 §7은 임베딩 모델 SentencePiece 토크나이저 기준. 본 구현은 PoC 근사치이며,
          품질 튜닝 단계에서 실제 토크나이저로 교체한다.
--------------------------------------------------
"""

import re

# 한글(가-힣) + 일본어 가나 + CJK 한자 — 글자 단위로 토큰 계산
_CJK_PATTERN = re.compile(r"[가-힣぀-ヿ一-鿿]")


def count_tokens(text: str) -> int:
    """텍스트의 대략적 토큰 수를 반환한다 (PoC 휴리스틱).

    CJK 문자는 글자 단위로, 그 외 텍스트는 공백 분리 토큰 수로 계산한다.

    Args:
        text: 토큰 수를 셀 텍스트.

    Returns:
        추정 토큰 수 (0 이상).
    """
    cjk_count = len(_CJK_PATTERN.findall(text))
    non_cjk = _CJK_PATTERN.sub(" ", text)
    return cjk_count + len(non_cjk.split())
