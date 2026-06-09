"""Queue Consumer — RabbitMQ 큐 소비 어댑터 [Storage 경계].

--------------------------------------------------
작성자 : 최태성
작성목적 : Worker 가 큐(content.chunking 등) 메시지를 소비할 때 pika 에 직접 결합하지
          않도록 하는 얇은 consumer 추상화. ABC + Fake + Pika 3계층(`app/CLAUDE.md` §8).
          Worker 핵심 로직(메시지 1건 처리)은 consumer 와 분리해 단위 테스트한다.
작성일 : 2026-05-26
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-26, 최초 작성, featureI-4 — MessageConsumer ABC + FakeMessageConsumer +
    PikaMessageConsumer. Chunking Worker 의 큐 소비 배선에 사용.
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - pika>=1.3 (PikaMessageConsumer 가 사용)
  - 외부 의존성 0 (base ABC + FakeMessageConsumer 는 pika 미설치 환경에서도 동작)
--------------------------------------------------
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any


class MessageConsumer(ABC):
    """큐 소비 추상 인터페이스 — Worker 가 메시지 스트림을 받는다."""

    @abstractmethod
    def consume(self) -> Iterator[dict[str, Any]]:
        """큐에서 메시지(JSON dict)를 순서대로 yield 한다."""


@dataclass(slots=True)
class FakeMessageConsumer(MessageConsumer):
    """In-memory ``MessageConsumer`` — 테스트·PoC 용(외부 의존성 0).

    미리 주입한 메시지 목록을 그대로 yield 한다(결정론). Worker end-to-end 테스트에서
    ``content.chunking`` 메시지 스트림을 재현한다.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)

    def consume(self) -> Iterator[dict[str, Any]]:
        yield from self.messages


class PikaMessageConsumer(MessageConsumer):
    """RabbitMQ(pika) 기반 consumer — 운영 경로.

    Args:
        channel: 사전 구성된 pika channel.
        queue: 소비할 큐 이름(예: ``content.chunking``).
        auto_ack: True 면 수신 즉시 ack. False(기본)면 호출자가 처리 성공 후 ack 한다
            (실패 메시지를 DLQ/재시도로 보내기 위함 — DLQ 정책은 후속).
    """

    def __init__(self, channel: Any, *, queue: str, auto_ack: bool = False) -> None:
        self._channel = channel
        self._queue = queue
        self._auto_ack = auto_ack

    def consume(self) -> Iterator[dict[str, Any]]:
        for method, _properties, body in self._channel.consume(
            self._queue, auto_ack=self._auto_ack
        ):
            message: dict[str, Any] = json.loads(body.decode("utf-8"))
            yield message
            if not self._auto_ack:
                self._channel.basic_ack(method.delivery_tag)
