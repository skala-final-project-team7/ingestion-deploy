"""Queue Publisher — RabbitMQ 라우팅 키 발행 어댑터 [Storage 경계].

--------------------------------------------------
작성자 : 최태성
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
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - pika>=1.3 (PikaQueuePublisher 가 사용)
  - 외부 의존성 0 (base ABC + FakeQueuePublisher 는 pika 미설치 환경에서도 동작)
--------------------------------------------------
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PublishedMessage:
    """발행된 메시지 1건 — 테스트 검증·로깅용 값 객체."""

    routing_key: str
    body: dict[str, Any]


class QueuePublisher(ABC):
    """큐 발행 추상 인터페이스 — crawler/sync 가 호출한다."""

    @abstractmethod
    def publish(self, *, routing_key: str, message: dict[str, Any]) -> None:
        """``routing_key`` 로 JSON 직렬화 가능한 ``message`` 를 발행한다."""


@dataclass(slots=True)
class FakeQueuePublisher(QueuePublisher):
    """In-memory ``QueuePublisher`` — 테스트·PoC 용(외부 의존성 0).

    발행 순서를 보존하는 list 로 적재하며, 테스트가 라우팅 키·메시지 형식을 검증한다.
    """

    messages: list[PublishedMessage] = field(default_factory=list)

    def publish(self, *, routing_key: str, message: dict[str, Any]) -> None:
        self.messages.append(PublishedMessage(routing_key=routing_key, body=dict(message)))


class PikaQueuePublisher(QueuePublisher):
    """RabbitMQ(pika) BlockingConnection 기반 publisher — 운영 경로.

    Args:
        channel: 사전 구성된 pika channel. ``exchange`` 로 ``routing_key`` 발행한다.
        exchange: 발행 exchange 이름(기본값은 default exchange "").
    """

    def __init__(self, channel: Any, *, exchange: str = "") -> None:
        self._channel = channel
        self._exchange = exchange

    def publish(self, *, routing_key: str, message: dict[str, Any]) -> None:
        # lazy import — pika 미설치 환경에서도 모듈 import 가 깨지지 않게(ABC/Fake 사용 경로).
        import pika

        body = json.dumps(message, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._channel.basic_publish(
            exchange=self._exchange,
            routing_key=routing_key,
            body=body,
            # persistent(delivery_mode=2) — durable queue 와 함께여야 브로커 재시작에도
            # 메시지가 살아남는다(A10 — completion event 유실 = Admin Key TTL 잔존).
            properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
        )
