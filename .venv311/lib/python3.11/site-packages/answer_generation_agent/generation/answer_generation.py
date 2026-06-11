from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Generation Agent LLM provider contract와 answer generation service 구현.
          feature4 범위에서는 raw answer/citation 후보까지만 생성하고 최종 citation mapping은 후속 feature로 둔다.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature4 LLM provider 및 generation service 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/protocol/json/os 기반
--------------------------------------------------
"""

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from answer_generation_agent.config import AnswerGenerationConfig
from answer_generation_agent.generation.input_normalization import (
    NormalizedGenerationInputResult,
)
from answer_generation_agent.generation.prompt_template import (
    PromptPayload,
    build_prompt_payload,
)
from answer_generation_agent.schemas import WarningItem

_REDACTION_MARKERS = (
    "OPENAI_API_KEY",
    "Authorization",
    "api key",
    "API key",
    "secret",
    "token",
    "synthetic-marker",
    "synthetic-external-key",
    "synthetic-injected-key",
)


class AnswerLLMProvider(Protocol):
    """Answer generation LLM provider interface."""

    provider_name: str

    def generate_answer(self, request: "AnswerGenerationRequest") -> "AnswerLLMResult":
        """LLM answer를 생성한다."""


@dataclass(slots=True)
class AnswerProviderError(Exception):
    """Safe provider error."""

    message: str
    retryable: bool
    error_type: str

    def __post_init__(self) -> None:
        self.message = _redact_text(self.message)
        Exception.__init__(self, self.message)

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return (
            "AnswerProviderError("
            f"message={self.message!r}, "
            f"retryable={self.retryable!r}, "
            f"error_type={self.error_type!r})"
        )


class ProviderConfigurationError(AnswerProviderError):
    """Provider configuration이 유효하지 않을 때 발생한다."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message=message,
            retryable=False,
            error_type="configuration_error",
        )


@dataclass(slots=True)
class OpenAITransportError(Exception):
    """OpenAI transport adapter가 반환하는 safe-able error."""

    status_code: int | None
    message: str


@dataclass(slots=True)
class RawSentenceCandidate:
    """Feature4에서 보존하는 raw sentence/citation candidate."""

    text: str
    citations: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.text = _redact_text(self.text)
        self.citations = [str(citation) for citation in self.citations]

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "citations": list(self.citations)}


@dataclass(slots=True)
class AnswerGenerationRequest:
    """LLM provider request."""

    prompt: PromptPayload
    model: str
    temperature: float
    timeout_seconds: int

    def to_provider_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
            "messages": [
                {"role": "system", "content": self.prompt.system_prompt},
                {"role": "developer", "content": self.prompt.developer_prompt},
                {"role": "user", "content": self.prompt.user_prompt},
            ],
        }

    def to_safe_dict(self) -> dict[str, Any]:
        return _sanitize_value(self.to_provider_dict())


@dataclass(slots=True)
class AnswerLLMResult:
    """Raw LLM provider result."""

    answer_text: str
    raw_sentence_candidates: list[RawSentenceCandidate] = field(default_factory=list)
    unsupported_gaps: list[str] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.answer_text:
            raise AnswerProviderError(
                message="answer is required in LLM response",
                retryable=False,
                error_type="invalid_response",
            )
        self.answer_text = _redact_text(self.answer_text)
        self.raw_sentence_candidates = [
            candidate
            if isinstance(candidate, RawSentenceCandidate)
            else RawSentenceCandidate(
                text=str(candidate.get("text") or ""),
                citations=_string_list(candidate.get("citations")),
            )
            for candidate in self.raw_sentence_candidates
        ]
        self.unsupported_gaps = [_redact_text(str(gap)) for gap in self.unsupported_gaps]
        self.raw_payload = _sanitize_value(self.raw_payload)


@dataclass(slots=True)
class AnswerGenerationResult:
    """Feature4 answer generation service result."""

    answer_status: str
    answer_text: str
    model: str
    provider_name: str
    prompt: PromptPayload
    raw_sentence_candidates: list[RawSentenceCandidate] = field(default_factory=list)
    unsupported_gaps: list[str] = field(default_factory=list)
    warnings: list[WarningItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_status": self.answer_status,
            "answer_text": _redact_text(self.answer_text),
            "model": self.model,
            "provider_name": self.provider_name,
            "prompt": self.prompt.to_dict(),
            "raw_sentence_candidates": [
                candidate.to_dict() for candidate in self.raw_sentence_candidates
            ],
            "unsupported_gaps": list(self.unsupported_gaps),
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


class FakeAnswerLLMProvider:
    """테스트용 fake provider. 네트워크 호출을 수행하지 않는다."""

    provider_name = "fake"

    def __init__(
        self,
        *,
        response: dict[str, Any] | None = None,
        raw_response: str | None = None,
        error: AnswerProviderError | None = None,
    ) -> None:
        self.response = response
        self.raw_response = raw_response
        self.error = error
        self.requests: list[AnswerGenerationRequest] = []

    def generate_answer(self, request: AnswerGenerationRequest) -> AnswerLLMResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.raw_response is not None:
            return parse_llm_response(self.raw_response)
        return parse_llm_response(self.response or {"answer": ""})


class OpenAIAnswerLLMProvider:
    """OpenAI provider adapter shell. 기본 테스트에서는 injectable transport만 사용한다."""

    provider_name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        transport: Any | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self._api_key:
            raise ProviderConfigurationError("OpenAI credential is required")
        self._transport = transport

    @property
    def has_api_key(self) -> bool:
        return bool(self._api_key)

    def __repr__(self) -> str:
        return "OpenAIAnswerLLMProvider(api_key=<redacted>)"

    def generate_answer(self, request: AnswerGenerationRequest) -> AnswerLLMResult:
        if self._transport is None:
            raise ProviderConfigurationError(
                "OpenAI transport is not configured for this runtime"
            )
        try:
            payload = self._transport(request.to_safe_dict())
        except OpenAITransportError as exc:
            raise _openai_error_to_provider_error(exc) from exc
        return parse_llm_response(payload)


class AnswerGenerationService:
    """Prompt builder와 provider를 연결하는 answer generation service."""

    def __init__(self, *, provider: AnswerLLMProvider) -> None:
        self.provider = provider

    def generate(
        self,
        *,
        normalized_input: NormalizedGenerationInputResult,
        config: AnswerGenerationConfig,
        use_fallback_model: bool = False,
    ) -> AnswerGenerationResult:
        prompt = build_prompt_payload(normalized_input)
        model = select_generation_model(config, use_fallback=use_fallback_model)
        warnings = list(prompt.warnings)

        if not normalized_input.normalized_contexts:
            warnings.append(
                WarningItem(
                    code="insufficient_context",
                    message="No usable context is available for answer generation.",
                )
            )
            return AnswerGenerationResult(
                answer_status="insufficient_context",
                answer_text="",
                model=model,
                provider_name=self.provider.provider_name,
                prompt=prompt,
                warnings=warnings,
            )

        if _is_weak_context(normalized_input):
            warnings.append(
                WarningItem(
                    code="weak_context",
                    message=(
                        "Top context has weak lexical overlap with the query; answer "
                        "must stay within supported evidence."
                    ),
                )
            )

        request = AnswerGenerationRequest(
            prompt=prompt,
            model=model,
            temperature=config.temperature,
            timeout_seconds=config.timeout_seconds,
        )
        llm_result = self.provider.generate_answer(request)
        return AnswerGenerationResult(
            answer_status="success",
            answer_text=llm_result.answer_text,
            model=model,
            provider_name=self.provider.provider_name,
            prompt=prompt,
            raw_sentence_candidates=llm_result.raw_sentence_candidates,
            unsupported_gaps=llm_result.unsupported_gaps,
            warnings=warnings,
        )


def select_generation_model(
    config: AnswerGenerationConfig,
    *,
    use_fallback: bool = False,
) -> str:
    """Config 기반 simple model policy."""
    return config.fallback_model if use_fallback else config.model


def parse_llm_response(raw_response: dict[str, Any] | str) -> AnswerLLMResult:
    """LLM raw response를 feature4 result schema로 검증/파싱한다."""
    payload: dict[str, Any]
    if isinstance(raw_response, str):
        try:
            loaded = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise AnswerProviderError(
                message="invalid LLM response JSON",
                retryable=False,
                error_type="invalid_response",
            ) from exc
        if not isinstance(loaded, dict):
            raise AnswerProviderError(
                message="invalid LLM response schema",
                retryable=False,
                error_type="invalid_response",
            )
        payload = loaded
    elif isinstance(raw_response, dict):
        payload = raw_response
    else:
        raise AnswerProviderError(
            message="invalid LLM response schema",
            retryable=False,
            error_type="invalid_response",
        )

    if not payload.get("answer"):
        raise AnswerProviderError(
            message="answer is required in LLM response",
            retryable=False,
            error_type="invalid_response",
        )

    sentences = payload.get("sentences") or []
    if not isinstance(sentences, list):
        raise AnswerProviderError(
            message="sentences must be a list in LLM response",
            retryable=False,
            error_type="invalid_response",
        )
    return AnswerLLMResult(
        answer_text=str(payload.get("answer") or ""),
        raw_sentence_candidates=[
            RawSentenceCandidate(
                text=str(item.get("text") or ""),
                citations=_string_list(item.get("citations")),
            )
            for item in sentences
            if isinstance(item, dict)
        ],
        unsupported_gaps=_string_list(payload.get("unsupported_gaps")),
        raw_payload=payload,
    )


def _openai_error_to_provider_error(error: OpenAITransportError) -> AnswerProviderError:
    status_code = error.status_code
    if status_code in {400, 401, 403}:
        return AnswerProviderError(
            message=error.message,
            retryable=False,
            error_type="auth_error" if status_code in {401, 403} else "request_error",
        )
    if status_code == 429:
        return AnswerProviderError(
            message=error.message,
            retryable=True,
            error_type="rate_limit_error",
        )
    if status_code is None:
        return AnswerProviderError(
            message=error.message,
            retryable=True,
            error_type="timeout_error",
        )
    if status_code >= 500:
        return AnswerProviderError(
            message=error.message,
            retryable=True,
            error_type="server_error",
        )
    return AnswerProviderError(
        message=error.message,
        retryable=False,
        error_type="provider_error",
    )


def _is_weak_context(normalized_input: NormalizedGenerationInputResult) -> bool:
    query_terms = _terms(normalized_input.generation_input.routing_decision.query)
    if not query_terms:
        return False
    context_terms: set[str] = set()
    for context in normalized_input.normalized_contexts:
        context_terms.update(_terms(context.title))
        context_terms.update(_terms(context.content))
    return query_terms.isdisjoint(context_terms)


def _terms(value: str) -> set[str]:
    return {
        token.lower()
        for token in value.replace("_", " ").replace("-", " ").split()
        if len(token) >= 3
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_redact_text(str(item)) for item in value if str(item).strip()]


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_value(item)
            for key, item in value.items()
            if not _is_sensitive_key(str(key))
        }
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(marker in normalized for marker in ("api_key", "authorization", "token", "secret"))


def _redact_text(text: str) -> str:
    redacted = text
    for marker in _REDACTION_MARKERS:
        redacted = redacted.replace(marker, "<redacted>")
    return redacted
