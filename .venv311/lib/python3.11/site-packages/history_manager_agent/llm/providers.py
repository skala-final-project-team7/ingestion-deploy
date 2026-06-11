from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : History Manager Agent LLM provider interface, fake provider,
          OpenAI provider adapter 구현.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature3 provider 구조 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/json/os/urllib 기반
--------------------------------------------------
"""

import json
import os
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from history_manager_agent.config import HistoryManagerConfig

OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"


class LLMProviderError(RuntimeError):
    """provider 오류를 retry 가능성과 safe message로 표현한다."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(_redact_sensitive_terms(message))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "status_code": self.status_code,
        }


class OpenAITransportError(RuntimeError):
    """OpenAI transport 계층에서 status code와 safe message를 전달하는 오류."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(_redact_sensitive_terms(message))


@dataclass(slots=True)
class HistoryClassificationRequest:
    """Provider가 사용할 classification request."""

    current_question: str
    prompt: str
    history_context: list[dict[str, Any]]
    model: str
    temperature: float
    timeout_seconds: int

    def to_openai_payload(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {
                    "role": "system",
                    "content": "You classify conversation history relation.",
                },
                {"role": "user", "content": self.prompt},
            ],
            "response_format": {"type": "json_object"},
        }

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "current_question": self.current_question,
            "prompt": self.prompt,
            "history_context": self.history_context,
            "model": self.model,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(slots=True)
class LLMProviderResponse:
    """Provider raw response wrapper."""

    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "metadata": self.metadata,
        }


class HistoryLLMProvider(Protocol):
    """History classification provider interface."""

    def classify_history(
        self,
        request: HistoryClassificationRequest,
    ) -> LLMProviderResponse:
        """Classification request를 실행하고 raw JSON content를 반환한다."""


class FakeHistoryLLMProvider:
    """기본 test suite용 fake provider."""

    def __init__(self, response: dict[str, Any] | str | Exception) -> None:
        self.response = response
        self.requests: list[HistoryClassificationRequest] = []

    def classify_history(
        self,
        request: HistoryClassificationRequest,
    ) -> LLMProviderResponse:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        if isinstance(self.response, str):
            return LLMProviderResponse(content=self.response)
        return LLMProviderResponse(content=json.dumps(self.response))


class OpenAIHistoryLLMProvider:
    """OpenAI Chat Completions 기반 provider.

    API key는 생성 시 외부 config/env에서 주입받고 repr/safe dict에는 포함하지 않는다.
    """

    def __init__(
        self,
        config: HistoryManagerConfig,
        api_key: str,
        transport: Callable[[HistoryClassificationRequest], str] | None = None,
    ) -> None:
        if not api_key:
            raise LLMProviderError(
                code="provider_configuration_error",
                message="OpenAI provider API key is not configured.",
                retryable=False,
            )
        self.config = config
        self._api_key = api_key
        self._transport = transport or self._default_transport

    def __repr__(self) -> str:
        return (
            "OpenAIHistoryLLMProvider("
            f"model={self.config.model!r}, timeout_seconds={self.config.timeout_seconds})"
        )

    @classmethod
    def from_config(
        cls,
        config: HistoryManagerConfig,
        env: Mapping[str, str] | None = None,
        transport: Callable[[HistoryClassificationRequest], str] | None = None,
    ) -> "OpenAIHistoryLLMProvider":
        source_env = os.environ if env is None else env
        api_key = config.openai_api_key or source_env.get("OPENAI_API_KEY") or ""
        return cls(config=config, api_key=api_key, transport=transport)

    def classify_history(
        self,
        request: HistoryClassificationRequest,
    ) -> LLMProviderResponse:
        try:
            content = self._transport(request)
        except TimeoutError as exc:
            raise LLMProviderError(
                code="openai_timeout",
                message="OpenAI provider request timed out.",
                retryable=True,
            ) from exc
        except OpenAITransportError as exc:
            raise _provider_error_from_status(exc.status_code, str(exc)) from exc
        return LLMProviderResponse(content=content)

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "provider": "openai",
            "config": self.config.to_safe_dict(),
        }

    def _default_transport(self, request: HistoryClassificationRequest) -> str:
        body = json.dumps(request.to_openai_payload()).encode("utf-8")
        http_request = urllib.request.Request(
            OPENAI_CHAT_COMPLETIONS_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                http_request,
                timeout=request.timeout_seconds,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (TimeoutError, socket.timeout) as exc:
            raise TimeoutError("OpenAI request timed out") from exc
        except urllib.error.HTTPError as exc:
            raise OpenAITransportError(
                status_code=exc.code,
                message="OpenAI HTTP request failed.",
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMProviderError(
                code="openai_network_error",
                message="OpenAI network request failed.",
                retryable=True,
            ) from exc

        return _extract_openai_content(payload)


def _provider_error_from_status(status_code: int, message: str) -> LLMProviderError:
    if status_code in {401, 403}:
        return LLMProviderError(
            code="openai_auth_error",
            message="OpenAI provider authentication failed.",
            retryable=False,
            status_code=status_code,
        )
    if status_code == 429 or status_code >= 500:
        return LLMProviderError(
            code="openai_retryable_error",
            message=message or "OpenAI provider retryable error.",
            retryable=True,
            status_code=status_code,
        )
    return LLMProviderError(
        code="openai_non_retryable_error",
        message=message or "OpenAI provider non-retryable error.",
        retryable=False,
        status_code=status_code,
    )


def _extract_openai_content(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMProviderError(
            code="openai_response_schema_error",
            message="OpenAI response schema is invalid.",
            retryable=False,
        ) from exc
    if not isinstance(content, str) or not content:
        raise LLMProviderError(
            code="openai_response_schema_error",
            message="OpenAI response content is empty.",
            retryable=False,
        )
    return content


def _redact_sensitive_terms(message: str) -> str:
    redacted = str(message)
    replacements = {
        "OPENAI_API_KEY": "<redacted>",
        "Authorization": "<redacted>",
        "Bearer": "<redacted>",
        "api key": "credential",
        "API key": "credential",
        "secret-like": "<redacted>",
    }
    for source, target in replacements.items():
        redacted = redacted.replace(source, target)
    return redacted
