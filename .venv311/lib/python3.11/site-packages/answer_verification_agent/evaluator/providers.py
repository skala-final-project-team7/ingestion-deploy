from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Answer Verification Agent evaluator provider 구현.
          feature6 범위에서는 fake evaluator와 OpenAI adapter를 provider interface 뒤에
          분리하고, 테스트는 injected transport로만 수행한다.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, evaluator provider/payload/parser 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/json/os/urllib 기반
--------------------------------------------------
"""

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from answer_verification_agent.config import AnswerVerificationConfig
from answer_verification_agent.evaluator.prompt import build_evaluator_prompt
from answer_verification_agent.schemas import SentenceLabel
from answer_verification_agent.schemas._serialization import to_primitive
from answer_verification_agent.verification.input_normalization import (
    NormalizedContext,
)
from answer_verification_agent.verification.suspicious_selector import (
    SuspiciousSentenceTarget,
)

_SENSITIVE_PATTERNS = [
    re.compile(r"OPENAI_API_KEY\s*=\s*[^,\s]+", re.IGNORECASE),
    re.compile(r"Authorization:\s*Bearer\s+[^,\s]+", re.IGNORECASE),
    re.compile(r"Bearer\s+[^,\s]+", re.IGNORECASE),
]
_OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"


@dataclass(slots=True)
class SentenceEvaluation:
    """LLM evaluator sentence result."""

    sentence_id: str
    label: SentenceLabel
    score: float
    reason: str
    unsupported_claims: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _redact_payload(to_primitive(self))


@dataclass(slots=True)
class OpenAITransportResponse:
    """Injected OpenAI transport response."""

    status_code: int
    body: dict[str, Any] | str


class EvaluatorProviderError(Exception):
    """Safe evaluator provider error."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        retryable: bool,
    ) -> None:
        super().__init__(_redact_text(message))
        self.error_type = error_type
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "retryable": self.retryable,
            "message": str(self),
        }


class AnswerEvaluatorProvider(Protocol):
    """Evaluator provider interface."""

    def evaluate_sentence(
        self,
        target: SuspiciousSentenceTarget,
        contexts: list[NormalizedContext],
    ) -> SentenceEvaluation:
        """Evaluate one sentence."""


OpenAITransport = Callable[[dict[str, Any]], OpenAITransportResponse]


class FakeEvaluatorProvider:
    """Deterministic fake evaluator for tests and local fixtures."""

    def __init__(
        self,
        scripted_results: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.scripted_results = scripted_results or {}

    def evaluate_sentence(
        self,
        target: SuspiciousSentenceTarget,
        contexts: list[NormalizedContext],
    ) -> SentenceEvaluation:
        _ = contexts
        payload = self.scripted_results.get(
            target.sentence_id,
            {
                "label": "LOW_CONFIDENCE",
                "score": target.score,
                "reason": "No scripted fake evaluation result was provided.",
                "unsupported_claims": [],
            },
        )
        return _evaluation_from_payload(payload, sentence_id=target.sentence_id)


class OpenAIEvaluatorProvider:
    """OpenAI evaluator adapter.

    실제 테스트는 injected transport만 사용한다. 기본 transport는 runtime에서만
    호출되며 API key는 환경변수 또는 config에서 외부 주입된 값을 사용한다.
    """

    def __init__(
        self,
        *,
        config: AnswerVerificationConfig,
        transport: OpenAITransport | None = None,
    ) -> None:
        self.config = config
        self.config.validate()
        self._api_key = config.openai_api_key or os.environ.get("OPENAI_API_KEY")
        if not self._api_key:
            raise EvaluatorProviderError(
                "OPENAI_API_KEY is required from external configuration.",
                error_type="configuration_error",
                retryable=False,
            )
        self._transport = transport or self._default_transport

    def evaluate_sentence(
        self,
        target: SuspiciousSentenceTarget,
        contexts: list[NormalizedContext],
    ) -> SentenceEvaluation:
        prompt = build_evaluator_prompt(target, contexts)
        request_payload = {
            "url": _OPENAI_CHAT_COMPLETIONS_URL,
            "headers": {
                "Authorization": "Bearer <redacted>",
                "Content-Type": "application/json",
            },
            "model": self.config.evaluator_model,
            "temperature": self.config.temperature,
            "timeout_seconds": self.config.timeout_seconds,
            "messages": [
                {"role": "system", "content": prompt.system_prompt},
                {"role": "user", "content": prompt.user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        try:
            response = self._transport(_redact_payload(request_payload))
        except TimeoutError as exc:
            raise EvaluatorProviderError(
                f"OpenAI evaluator timeout: {exc}",
                error_type="timeout",
                retryable=True,
            ) from exc
        except EvaluatorProviderError:
            raise
        except Exception as exc:
            raise EvaluatorProviderError(
                f"OpenAI evaluator request failed: {exc}",
                error_type="transport_error",
                retryable=True,
            ) from exc

        _raise_for_status(response)
        return parse_evaluator_response(
            _extract_openai_content(response.body),
            sentence_id=target.sentence_id,
        )

    def _default_transport(
        self,
        request_payload: dict[str, Any],
    ) -> OpenAITransportResponse:
        body = json.dumps(
            {
                "model": request_payload["model"],
                "temperature": request_payload["temperature"],
                "messages": request_payload["messages"],
                "response_format": request_payload["response_format"],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            _OPENAI_CHAT_COMPLETIONS_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - runtime adapter; tests inject transport.
                request,
                timeout=self.config.timeout_seconds,
            ) as response:
                response_body = json.loads(response.read().decode("utf-8"))
                return OpenAITransportResponse(
                    status_code=response.status,
                    body=response_body,
                )
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8")
            try:
                parsed_body: dict[str, Any] | str = json.loads(error_body)
            except json.JSONDecodeError:
                parsed_body = error_body
            return OpenAITransportResponse(status_code=exc.code, body=parsed_body)


def parse_evaluator_response(
    value: str | dict[str, Any],
    *,
    sentence_id: str,
) -> SentenceEvaluation:
    """Evaluator JSON response를 sentence evaluation으로 파싱한다."""
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise EvaluatorProviderError(
                "Evaluator response was not valid JSON.",
                error_type="invalid_response",
                retryable=False,
            ) from exc
    elif isinstance(value, dict):
        payload = value
    else:
        raise EvaluatorProviderError(
            "Evaluator response must be a JSON object.",
            error_type="invalid_response",
            retryable=False,
        )
    return _evaluation_from_payload(payload, sentence_id=sentence_id)


def _evaluation_from_payload(
    payload: dict[str, Any],
    *,
    sentence_id: str,
) -> SentenceEvaluation:
    label = _normalize_label(payload.get("label"))
    score = _normalize_score(payload.get("score"))
    reason = str(payload.get("reason") or "Evaluator did not provide a reason.")
    unsupported_claims = [
        str(item)
        for item in payload.get("unsupported_claims") or []
        if str(item).strip()
    ]
    return SentenceEvaluation(
        sentence_id=sentence_id,
        label=label,
        score=score,
        reason=_redact_text(reason),
        unsupported_claims=[_redact_text(item) for item in unsupported_claims],
        raw=_redact_payload(dict(payload)),
    )


def _normalize_label(value: Any) -> SentenceLabel:
    try:
        label = SentenceLabel(str(value))
    except ValueError:
        return SentenceLabel.LOW_CONFIDENCE
    if label == SentenceLabel.NOT_CHECKED:
        return SentenceLabel.LOW_CONFIDENCE
    return label


def _normalize_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, round(score, 4)))


def _extract_openai_content(body: dict[str, Any] | str) -> str:
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError as exc:
            raise EvaluatorProviderError(
                "OpenAI response body was not valid JSON.",
                error_type="invalid_response",
                retryable=False,
            ) from exc
    try:
        return str(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise EvaluatorProviderError(
            "OpenAI response did not include evaluator content.",
            error_type="invalid_response",
            retryable=False,
        ) from exc


def _raise_for_status(response: OpenAITransportResponse) -> None:
    if 200 <= response.status_code < 300:
        return
    message = _response_error_message(response.body)
    if response.status_code in {401, 403}:
        raise EvaluatorProviderError(
            f"OpenAI evaluator auth error: {message}",
            error_type="auth_error",
            retryable=False,
        )
    if response.status_code == 429 or response.status_code >= 500:
        raise EvaluatorProviderError(
            f"OpenAI evaluator retryable error: {message}",
            error_type="retryable_http_error",
            retryable=True,
        )
    raise EvaluatorProviderError(
        f"OpenAI evaluator non-retryable error: {message}",
        error_type="http_error",
        retryable=False,
    )


def _response_error_message(body: dict[str, Any] | str) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return _redact_text(str(error.get("message") or "unknown error"))
        return _redact_text(str(body))
    return _redact_text(body)


def _redact_payload(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_payload(item) for key, item in value.items()}
    return value


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub("<redacted>", redacted)
    return redacted
