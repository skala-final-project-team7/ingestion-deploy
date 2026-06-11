from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : BFF conversation history JSON loading 및 normalization/trimming service.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature2 history input normalization 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 json/pathlib/dataclasses 기반
--------------------------------------------------
"""

import json
from dataclasses import dataclass, field
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from history_manager_agent.config import HistoryManagerConfig
from history_manager_agent.schemas import (
    ConversationRole,
    ConversationTurn,
    HistoryManagerInput,
    HistoryWarning,
)

FALLBACK_CREATED_AT = "0001-01-01T00:00:00Z"


class HistoryInputLoaderError(ValueError):
    """History input file을 job 실행 불가능 상태로 만드는 loader 오류."""


@dataclass(slots=True)
class NormalizedHistoryResult:
    """Normalization/trimming 결과.

    `history_input`은 current question 등 top-level contract 보존용이고, `to_dict()`는
    후속 context/output에 전체 input history가 복제되지 않도록 summary 형태만 반환한다.
    """

    history_input: HistoryManagerInput
    normalized_history: list[ConversationTurn]
    input_turn_count: int
    warnings: list[HistoryWarning] = field(default_factory=list)

    @property
    def used_turn_count(self) -> int:
        return len(self.normalized_history)

    def to_llm_context_turns(
        self,
        include_system: bool = False,
    ) -> list[ConversationTurn]:
        """LLM 판단 입력용 turn 목록을 반환한다.

        system turn은 normalized history에는 보존하되, 기본 판단 입력에서는 제외한다.
        """
        if include_system:
            return list(self.normalized_history)
        return [
            turn
            for turn in self.normalized_history
            if turn.role != ConversationRole.SYSTEM
        ]

    def to_dict(self) -> dict[str, Any]:
        """로그/report/output에 안전하게 사용할 primitive dictionary를 반환한다."""
        return {
            "conversation_id": self.history_input.conversation_id,
            "user_id": self.history_input.user_id,
            "current_question": self.history_input.current_question,
            "normalized_history": [
                turn.to_dict() for turn in self.normalized_history
            ],
            "input_turn_count": self.input_turn_count,
            "used_turn_count": self.used_turn_count,
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


def load_history_input(input_path: str | Path) -> HistoryManagerInput:
    """JSON file에서 strict HistoryManagerInput schema를 로드한다.

    feature2의 tolerant turn 처리는 `load_and_normalize_history_input()`에서 담당한다.
    """
    payload = _read_input_payload(input_path)
    return HistoryManagerInput.from_dict(payload)


def load_and_normalize_history_input(
    input_path: str | Path,
    config: HistoryManagerConfig,
) -> NormalizedHistoryResult:
    """JSON file을 로드한 뒤 tolerant history normalization을 수행한다."""
    payload = _read_input_payload(input_path)
    return normalize_history_input_payload(payload, config)


def normalize_history_input(
    history_input: HistoryManagerInput,
    config: HistoryManagerConfig,
) -> NormalizedHistoryResult:
    """이미 strict schema로 생성된 input의 history를 정렬/trimming한다."""
    payload = history_input.to_dict()
    return normalize_history_input_payload(payload, config)


def normalize_history_input_payload(
    payload: dict[str, Any],
    config: HistoryManagerConfig,
) -> NormalizedHistoryResult:
    """primitive input payload를 tolerant policy로 normalization한다."""
    config.validate()
    history_items = payload.get("history") or []
    if not isinstance(history_items, list):
        raise ValueError("history must be a list")

    top_level_input = _build_top_level_input(payload)
    normalized_turns, warnings = _normalize_turns(history_items)
    normalized_turns.sort(key=lambda item: item[0])
    turns = [turn for _, turn in normalized_turns]

    turns = _trim_history_window(turns, config.history_window_turns, warnings)
    turns = _trim_context_chars(turns, config.max_context_chars, warnings)

    top_level_input.history = turns
    return NormalizedHistoryResult(
        history_input=top_level_input,
        normalized_history=turns,
        input_turn_count=len(history_items),
        warnings=warnings,
    )


def _read_input_payload(input_path: str | Path) -> dict[str, Any]:
    path = Path(input_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HistoryInputLoaderError("Input JSON file not found") from exc
    except JSONDecodeError as exc:
        raise HistoryInputLoaderError("Malformed input JSON") from exc

    if not isinstance(payload, dict):
        raise ValueError("input JSON must be an object")
    return payload


def _build_top_level_input(payload: dict[str, Any]) -> HistoryManagerInput:
    return HistoryManagerInput(
        conversation_id=str(payload.get("conversation_id") or ""),
        user_id=str(payload.get("user_id") or ""),
        current_question=str(payload.get("current_question") or ""),
        history=[],
        metadata=payload.get("metadata") or {},
    )


def _normalize_turns(
    history_items: list[Any],
) -> tuple[list[tuple[tuple[str, int], ConversationTurn]], list[HistoryWarning]]:
    normalized: list[tuple[tuple[str, int], ConversationTurn]] = []
    warnings: list[HistoryWarning] = []

    for index, item in enumerate(history_items):
        turn = _normalize_turn(item, index, warnings)
        if turn is None:
            continue
        normalized.append(((turn.created_at, index), turn))

    return normalized, warnings


def _normalize_turn(
    item: Any,
    index: int,
    warnings: list[HistoryWarning],
) -> ConversationTurn | None:
    if not isinstance(item, dict):
        warnings.append(
            HistoryWarning(
                code="invalid_turn",
                message="History turn was skipped because it is not an object.",
            )
        )
        return None

    turn_id = str(item.get("turn_id") or "")
    role_value = str(item.get("role") or "").strip().lower()
    content = str(item.get("content") or "")
    created_at = str(item.get("created_at") or "")

    if not turn_id or not content:
        warnings.append(
            HistoryWarning(
                code="invalid_turn",
                message="History turn was skipped because required fields are missing.",
                turn_id=turn_id or None,
            )
        )
        return None

    try:
        role = ConversationRole(role_value)
    except ValueError:
        warnings.append(
            HistoryWarning(
                code="invalid_role",
                message="History turn was skipped because its role is not supported.",
                turn_id=turn_id,
            )
        )
        return None

    if not created_at:
        created_at = FALLBACK_CREATED_AT
        warnings.append(
            HistoryWarning(
                code="missing_created_at",
                message="History turn used deterministic fallback created_at.",
                turn_id=turn_id,
            )
        )

    try:
        return ConversationTurn(
            turn_id=turn_id,
            role=role,
            content=content,
            created_at=created_at,
            citations=item.get("citations") or [],
            metadata=item.get("metadata") or {},
        )
    except ValueError:
        warnings.append(
            HistoryWarning(
                code="invalid_turn",
                message="History turn was skipped because schema validation failed.",
                turn_id=turn_id,
            )
        )
        return None


def _trim_history_window(
    turns: list[ConversationTurn],
    history_window_turns: int,
    warnings: list[HistoryWarning],
) -> list[ConversationTurn]:
    if len(turns) <= history_window_turns:
        return turns
    warnings.append(
        HistoryWarning(
            code="history_window_trimmed",
            message="History was trimmed to the configured recent turn window.",
        )
    )
    return turns[-history_window_turns:]


def _trim_context_chars(
    turns: list[ConversationTurn],
    max_context_chars: int,
    warnings: list[HistoryWarning],
) -> list[ConversationTurn]:
    if _content_chars(turns) <= max_context_chars:
        return turns

    trimmed = list(turns)
    while len(trimmed) > 1 and _content_chars(trimmed) > max_context_chars:
        trimmed.pop(0)

    if len(trimmed) != len(turns):
        warnings.append(
            HistoryWarning(
                code="max_context_chars_trimmed",
                message="History was trimmed to fit max_context_chars.",
            )
        )
    return trimmed


def _content_chars(turns: list[ConversationTurn]) -> int:
    return sum(len(turn.content) for turn in turns)
