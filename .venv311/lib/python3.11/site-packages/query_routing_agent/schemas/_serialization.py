from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Query Routing Agent schema 직렬화 helper.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, dataclass/enum primitive 변환 helper 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/enum 기반
--------------------------------------------------
"""

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any


def to_primitive(value: Any) -> Any:
    """JSON 직렬화 가능한 primitive 값으로 변환한다."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: to_primitive(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_primitive(item) for item in value]
    return value
