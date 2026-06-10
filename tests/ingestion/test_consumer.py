"""PikaMessageConsumer 회귀 — malformed body poison 격리 (배포 전 점검 2026-06-10).

비 JSON·비 dict body 는 재시도해도 해소되지 않는 영구 실패다. generator 밖으로
전파되면 Worker 루프가 미ack 상태로 죽고 브로커 재전송 crash 루프가 되므로(A4 와
같은 뿌리), consumer 가 ``basic_nack(requeue=False)`` 로 거부하고 다음 메시지로
진행하는지 검증한다(DLX 구성 시 DLQ 이동).
"""

from __future__ import annotations

from typing import Any

from app.ingestion.workers.consumer import PikaMessageConsumer


class _FakeMethod:
    def __init__(self, delivery_tag: int) -> None:
        self.delivery_tag = delivery_tag


class _FakePikaChannel:
    """pika channel 의 consume/ack/nack 표면만 흉내내는 fake."""

    def __init__(self, bodies: list[bytes]) -> None:
        self._bodies = bodies
        self.acked: list[int] = []
        self.nacked: list[tuple[int, bool]] = []

    def consume(self, queue: str, auto_ack: bool = False) -> Any:
        for index, body in enumerate(self._bodies):
            yield _FakeMethod(index), None, body

    def basic_ack(self, delivery_tag: int) -> None:
        self.acked.append(delivery_tag)

    def basic_nack(self, delivery_tag: int, requeue: bool = True) -> None:
        self.nacked.append((delivery_tag, requeue))


def test_malformed_bodies_rejected_and_valid_message_still_consumed() -> None:
    channel = _FakePikaChannel(
        bodies=[
            b"not-json{{{",  # 비 JSON
            b'["list", "not", "dict"]',  # JSON 이지만 비 dict
            b'{"page_id": "p-1", "source_type": "page"}',  # 정상
        ]
    )
    consumer = PikaMessageConsumer(channel, queue="content.chunking")

    messages = list(consumer.consume())

    assert messages == [{"page_id": "p-1", "source_type": "page"}]
    # malformed 2건은 requeue=False 로 거부됐다(재전송 루프 차단, DLX 시 DLQ 이동).
    assert channel.nacked == [(0, False), (1, False)]
    # 정상 1건만 ack 됐다.
    assert channel.acked == [2]


def test_auto_ack_mode_skips_manual_ack_and_nack() -> None:
    channel = _FakePikaChannel(bodies=[b"broken", b'{"page_id": "p-2"}'])
    consumer = PikaMessageConsumer(channel, queue="content.chunking", auto_ack=True)

    messages = list(consumer.consume())

    assert messages == [{"page_id": "p-2"}]
    # auto_ack 모드 — 브로커가 이미 ack 했으므로 수동 ack/nack 을 호출하지 않는다.
    assert channel.acked == []
    assert channel.nacked == []
