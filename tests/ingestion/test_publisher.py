"""QueuePublisher 단위 테스트 — 발행 재시도/로그 민감정보 마스킹 검증."""

from __future__ import annotations

import json
import logging

import pytest

from app.ingestion.workers.publisher import (
    FakeQueuePublisher,
    PikaQueuePublisher,
    _PUBLISH_RETRY_ATTEMPTS,
)


class _FakePikaChannel:
    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls: list[dict[str, object]] = []
        self.published = 0

    def basic_publish(self, *, exchange: str, routing_key: str, body: bytes, properties: object) -> None:
        self.published += 1
        self.calls.append(
            {
                "exchange": exchange,
                "routing_key": routing_key,
                "body": body,
                "properties": properties,
            }
        )
        if self.published <= self.fail_times:
            raise RuntimeError("temporary publish failure")


def test_fake_queue_publisher_records_message_body() -> None:
    publisher = FakeQueuePublisher()
    publisher.publish(routing_key="rk", message={"a": 1, "b": "2"})

    assert len(publisher.messages) == 1
    assert publisher.messages[0].routing_key == "rk"
    assert publisher.messages[0].body == {"a": 1, "b": "2"}


def test_pika_queue_publisher_sets_persistent_delivery_and_json_content_type() -> None:
    channel = _FakePikaChannel()
    publisher = PikaQueuePublisher(channel)
    publisher.publish(
        routing_key="rk",
        message={"jobId": "job-0", "adminUserId": "admin-0"},
        max_attempts=1,
    )

    published = channel.calls[0]["properties"]
    assert getattr(published, "delivery_mode", None) == 2
    assert getattr(published, "content_type", None) == "application/json"


def test_pika_queue_publisher_retries_until_success() -> None:
    channel = _FakePikaChannel(fail_times=1)
    publisher = PikaQueuePublisher(channel)
    publisher.publish(
        routing_key="rk",
        message={"jobId": "job-1", "adminUserId": "admin-1"},
        max_attempts=3,
        retry_backoff_seconds=0,
    )

    assert channel.published == 2
    assert channel.calls[0]["routing_key"] == "rk"
    payload = json.loads(channel.calls[-1]["body"].decode("utf-8"))
    assert payload["jobId"] == "job-1"


def test_pika_queue_publisher_raises_after_retries_and_logs_without_secrets(caplog: pytest.LogCaptureFixture) -> None:
    channel = _FakePikaChannel(fail_times=3)
    publisher = PikaQueuePublisher(channel)
    message = {
        "jobId": "job-2",
        "adminUserId": "admin-2",
        "accessToken": "very-sensitive-token",
        "cloudId": "very-sensitive-cloud",
        "adminEmail": "admin@example.com",
    }

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="temporary publish failure"):
        publisher.publish(
            routing_key="rk",
            message=message,
            max_attempts=2,
            retry_backoff_seconds=0,
        )

    assert channel.published == 2
    assert "very-sensitive-token" not in caplog.text
    assert "very-sensitive-cloud" not in caplog.text
    assert "admin@example.com" not in caplog.text
    assert "accessToken" in caplog.text  # 키 자체는 표시되더라도 값은 마스킹되어야 함.
    assert "<redacted>" in caplog.text


def test_publish_config_has_default_retry_attempts() -> None:
    assert _PUBLISH_RETRY_ATTEMPTS >= 2
