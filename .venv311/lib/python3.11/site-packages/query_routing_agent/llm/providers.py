from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Query Routing Agent LLM provider interface, fake provider,
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

from query_routing_agent.config import QueryRoutingConfig

OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
SENSITIVE_MARKERS = (
    "OPENAI_API_KEY",
    "Authorization",
    "Bearer",
    "api_key",
    "api key",
    "access_token",
    "token",
    "secret",
)


class RoutingProviderError(RuntimeError):
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
        """Safe error dictionary를 반환한다."""
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "status_code": self.status_code,
        }


class OpenAITransportError(RuntimeError):
    """OpenAI transport 계층 status code와 safe message를 전달하는 오류."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(_redact_sensitive_terms(message))


@dataclass(slots=True)
class RoutingClassificationRequest:
    """Provider가 사용할 routing classification request."""

    query: str
    prompt: str
    routing_input: dict[str, Any]
    model: str
    temperature: float
    timeout_seconds: int

    def to_openai_payload(self) -> dict[str, Any]:
        """OpenAI Chat Completions payload를 구성한다."""
        return {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {
                    "role": "system",
                    "content": "You classify RAG query routing intent.",
                },
                {"role": "user", "content": self.prompt},
            ],
            "response_format": {"type": "json_object"},
        }

    def to_safe_dict(self) -> dict[str, Any]:
        """로그/report용 safe dictionary를 반환한다."""
        return {
            "query": self.query,
            "prompt": self.prompt,
            "routing_input": self.routing_input,
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
        """로그/report용 safe dictionary를 반환한다."""
        return {"content": self.content, "metadata": self.metadata}


class RoutingLLMProvider(Protocol):
    """Query routing classification provider interface."""

    def route_query(
        self,
        request: RoutingClassificationRequest,
    ) -> LLMProviderResponse:
        """Routing request를 실행하고 raw JSON content를 반환한다."""


class FakeRoutingLLMProvider:
    """기본 test suite용 fake provider."""

    def __init__(self, response: dict[str, Any] | str | Exception) -> None:
        self.response = response
        self.requests: list[RoutingClassificationRequest] = []

    def route_query(
        self,
        request: RoutingClassificationRequest,
    ) -> LLMProviderResponse:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        if isinstance(self.response, str):
            return LLMProviderResponse(content=self.response)
        return LLMProviderResponse(content=json.dumps(self.response))


class OpenAIRoutingLLMProvider:
    """OpenAI Chat Completions 기반 provider.

    API key는 생성 시 외부 config/env에서 주입받고 repr/safe dict에는 포함하지 않는다.
    """

    def __init__(
        self,
        config: QueryRoutingConfig,
        api_key: str,
        transport: Callable[[RoutingClassificationRequest], str] | None = None,
    ) -> None:
        if not api_key:
            raise RoutingProviderError(
                code="provider_configuration_error",
                message="OpenAI provider key is not configured.",
                retryable=False,
            )
        self.config = config
        self._api_key = api_key
        self._transport = transport or self._default_transport

    def __repr__(self) -> str:
        return (
            "OpenAIRoutingLLMProvider("
            f"model={self.config.model!r}, timeout_seconds={self.config.timeout_seconds})"
        )

    @classmethod
    def from_config(
        cls,
        config: QueryRoutingConfig,
        env: Mapping[str, str] | None = None,
        transport: Callable[[RoutingClassificationRequest], str] | None = None,
    ) -> "OpenAIRoutingLLMProvider":
        source_env = os.environ if env is None else env
        api_key = config.openai_api_key or source_env.get("OPENAI_API_KEY") or ""
        return cls(config=config, api_key=api_key, transport=transport)

    def route_query(
        self,
        request: RoutingClassificationRequest,
    ) -> LLMProviderResponse:
        try:
            content = self._transport(request)
        except TimeoutError as exc:
            raise RoutingProviderError(
                code="openai_timeout",
                message="OpenAI provider request timed out.",
                retryable=True,
            ) from exc
        except OpenAITransportError as exc:
            raise _provider_error_from_status(exc.status_code, str(exc)) from exc
        return LLMProviderResponse(content=content)

    def to_safe_dict(self) -> dict[str, Any]:
        """Provider safe metadata를 반환한다."""
        return {"provider": "openai", "config": self.config.to_safe_dict()}

    def _default_transport(self, request: RoutingClassificationRequest) -> str:
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
        except urllib.error.HTTPError as exc:
            raise OpenAITransportError(exc.code, "OpenAI HTTP error") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise TimeoutError("OpenAI request timed out") from exc
        except urllib.error.URLError as exc:
            raise RoutingProviderError(
                code="openai_network_error",
                message="OpenAI provider network error.",
                retryable=True,
            ) from exc

        try:
            return str(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RoutingProviderError(
                code="openai_schema_error",
                message="OpenAI provider returned an unexpected response.",
                retryable=False,
            ) from exc


def _provider_error_from_status(
    status_code: int,
    message: str,
) -> RoutingProviderError:
    if status_code in {401, 403}:
        return RoutingProviderError(
            code="openai_auth_error",
            message=message,
            retryable=False,
            status_code=status_code,
        )
    if status_code == 429 or status_code >= 500:
        return RoutingProviderError(
            code="openai_retryable_error",
            message=message,
            retryable=True,
            status_code=status_code,
        )
    return RoutingProviderError(
        code="openai_non_retryable_error",
        message=message,
        retryable=False,
        status_code=status_code,
    )


def _redact_sensitive_terms(message: str) -> str:
    text = str(message)
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in SENSITIVE_MARKERS):
        return "Provider error occurred."
    return text
