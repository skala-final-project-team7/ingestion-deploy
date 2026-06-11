from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any


def to_primitive(value: Any) -> Any:
    """dataclass, enum, path를 JSON 직렬화 가능한 primitive 값으로 변환한다."""
    if is_dataclass(value):
        return to_primitive(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: to_primitive(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_primitive(item) for item in value]
    return value
