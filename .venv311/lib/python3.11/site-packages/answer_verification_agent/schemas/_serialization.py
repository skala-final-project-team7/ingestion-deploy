from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Verification Agent schema 객체의 안전한 primitive serialization helper.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, dataclass/enum/list/dict serialization 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/enum 기반
--------------------------------------------------
"""

from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any


def to_primitive(value: Any) -> Any:
    """dataclass, enum, collection을 JSON 호환 primitive로 변환한다."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: to_primitive(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, list):
        return [to_primitive(item) for item in value]
    if isinstance(value, tuple):
        return [to_primitive(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_primitive(item) for key, item in value.items()}
    return value
