from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any


def to_primitive(value: Any) -> Any:
    """dataclass/enum/list/dict 값을 JSON 직렬화 가능한 primitive로 변환한다."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: to_primitive(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, list):
        return [to_primitive(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_primitive(item) for key, item in value.items()}
    return value
