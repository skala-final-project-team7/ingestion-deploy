"""Queue Publisher — RabbitMQ 라우팅 키 발행 어댑터 [Storage 경계].

--------------------------------------------------
작성자 : 최태성, 이다연
담당 영역 : ingestion
작성목적 : 수집/동기화 단계가 다음 단계 큐(Chunking 등)로 메시지를 발행하기 위한 얇은
          publisher 추상화. 비즈니스 로직(crawler/sync)이 pika 에 직접 결합하지 않도록
          ABC + Fake + Pika 3계층으로 분리한다(`app/CLAUDE.md` §8). 메시지 페이로드에는
          토큰·자격증명을 절대 싣지 않는다(루트 CLAUDE.md 보안 규칙).
작성일 : 2026-05-26
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-26, 최초 작성, featureI-6 — QueuePublisher ABC + FakeQueuePublisher +
    PikaQueuePublisher. crawler 의 Chunking Queue 발행 배선에 사용.
  - 2026-06-10, 코드 리뷰 재점검(A10) — PikaQueuePublisher 발행을 persistent
    (delivery_mode=2)로 변경. durable queue 여도 non-persistent 메시지는 브로커
    재시작 시 유실된다(completion event = Admin Key 말소 트리거라 유실 불가).
  - 2026-06-11, featureI-7c Step5 적용 — completion event 발행의 장애 대응 정합성 보정:
    publish 실패 시 bounded retry(기본 3회) 및 backoff 적용, 실패 로그에 민감 키
    마스킹 메시지를 기록해 보안/운영 추적성 동시 확보.
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - pika>=1.3 (PikaQueuePublisher 가 사용)
  - 외부 의존성 0 (base ABC + FakeQueuePublisher 는 pika 미설치 환경에서도 동작)
--------------------------------------------------
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, cast

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PublishedMessage:
    """발행된 메시지 1건 — 테스트 검증·로깅용 값 객체."""

    routing_key: str
    body: dict[str, Any]


_DEFAULT_EXCHANGE = ""
"""completion event 기본 발행 exchange.

featureI-7c 기준으로 Data Ingestion completion 이벤트는 default exchange("")를 사용.
"""
_DEFAULT_ROUTING_KEY = "lina.admin.ingest.completion"
"""completion event 기본 routing key.

설정값을 명시적으로 주입하지 않더라도 BFF 기준 큐(`lina.admin.ingest.completion`)로
직접 바인딩되지 않아도 라우팅되도록 하는 기본값.
"""
_DELIVERY_MODE_PERSISTENT = 2
"""AMQP deliveryMode = 2(PERSISTENT).

completion 이벤트는 Admin Key deactivate 트리거이므로 브로커 재시작 시 유실을 방지.
"""
_CONTENT_TYPE_JSON = "application/json"
"""completion payload의 표준 content-type."""

_SENSITIVE_FIELD_NAMES = ("accessToken", "refreshToken", "cloudId", "adminApiToken", "adminEmail")
"""publisher 로그에서 마스킹해야 할 field 키(대소문자 구분 안 함)."""

_PUBLISH_RETRY_ATTEMPTS = 3
"""publisher retry 기본 횟수."""

_PUBLISH_RETRY_DELAY_SECONDS = 0.2
"""publisher retry 기본 대기(sec)."""


def _sanitize_for_log(message: dict[str, Any]) -> dict[str, Any]:
    """로그에 노출하기 전 민감 필드를 마스킹한 dict 를 생성한다."""
    sanitized: dict[str, Any] = {}
    for key, value in message.items():
        if key.lower() in (name.lower() for name in _SENSITIVE_FIELD_NAMES):
            sanitized[key] = "<redacted>"
        else:
            sanitized[key] = value
    return sanitized


def _json_body(message: dict[str, Any]) -> bytes:
    return json.dumps(message, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _publish_properties(*, pika_module: Any) -> Any:
    return pika_module.BasicProperties(
        delivery_mode=_DELIVERY_MODE_PERSISTENT,
        content_type=_CONTENT_TYPE_JSON,
    )


class QueuePublisher(ABC):
    """큐 발행 추상 인터페이스 — crawler/sync 가 호출한다."""

    @abstractmethod
    def publish(self, *, routing_key: str = _DEFAULT_ROUTING_KEY, message: dict[str, Any]) -> None:
        """``routing_key`` 로 JSON 직렬화 가능한 ``message`` 를 발행한다.

        기본값은 completion 이벤트 계약(``default exchange +
        lina.admin.ingest.completion``)을 따르되, 기존 청크/동기화 큐 경로의
        호출자는 라우팅 키를 전달해 상위 계약을 오버라이드할 수 있다.
        """


@dataclass(slots=True)
class FakeQueuePublisher(QueuePublisher):
    """In-memory ``QueuePublisher`` — 테스트·PoC 용(외부 의존성 0).

    발행 순서를 보존하는 list 로 적재하며, 테스트가 라우팅 키·메시지 형식을 검증한다.
    """

    messages: list[PublishedMessage] = field(default_factory=list)

    def publish(self, *, routing_key: str = _DEFAULT_ROUTING_KEY, message: dict[str, Any]) -> None:
        # 테스트/PoC 경로에서는 실제 RabbitMQ 없이 기본 계약 경로와 동일한 기본값을 보존한다.
        self.messages.append(PublishedMessage(routing_key=routing_key, body=dict(message)))


class PikaQueuePublisher(QueuePublisher):
    """RabbitMQ(pika) BlockingConnection 기반 publisher — 운영 경로.

    Args:
        channel: 사전 구성된 pika channel. ``exchange`` 로 ``routing_key`` 발행한다.
        exchange: 발행 exchange 이름(기본값은 default exchange "").
    """

    def __init__(self, channel: Any, *, exchange: str = _DEFAULT_EXCHANGE) -> None:
        # featureI-7c: completion 이벤트 경로의 기본 계약은 exchange="".
        self._channel = channel
        self._exchange = exchange

    def publish(
        self,
        *,
        routing_key: str = _DEFAULT_ROUTING_KEY,
        message: dict[str, Any],
        max_attempts: int = _PUBLISH_RETRY_ATTEMPTS,
        retry_backoff_seconds: float = _PUBLISH_RETRY_DELAY_SECONDS,
    ) -> None:
        # featureI-7c: completion 이벤트 기본 라우팅 경로 + persistence/contentType 정합.
        # lazy import — pika 미설치 환경에서도 모듈 import 가 깨지지 않게(ABC/Fake 사용 경로).
        import pika

        attempts = max(1, int(max_attempts))
        body = _json_body(message)
        properties = _publish_properties(pika_module=pika)
        for attempt in range(1, attempts + 1):
            try:
                self._channel.basic_publish(
                    exchange=self._exchange,
                    routing_key=routing_key,
                    body=body,
                    properties=cast(Any, properties),
                )
                return
            except Exception:  # noqa: BLE001 — 메시지 발행 오류는 재시도/상위 로깅으로 처리
                if attempt >= attempts:
                    sanitized_message = _sanitize_for_log(message)
                    _LOGGER.exception(
                        "Failed to publish message to exchange=%r routing_key=%r "
                        "after %d attempts. "
                        "message_keys=%r payload_excerpt=%r",
                        self._exchange,
                        routing_key,
                        attempts,
                        list(sanitized_message.keys()),
                        sanitized_message,
                    )
                    raise
                _LOGGER.warning(
                    "publish retry %d/%d: exchange=%r routing_key=%r message_keys=%r",
                    attempt,
                    attempts,
                    self._exchange,
                    routing_key,
                    list(_sanitize_for_log(message).keys()),
                )
                time.sleep(retry_backoff_seconds)
