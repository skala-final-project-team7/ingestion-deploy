"""completion 이벤트 단독 테스트.

- jobId/adminUserId 필드 누락 처리와 status 제약을 검증한다.
- completion 이벤트 발행 시 AMQP 메시지가 persistent(2) 및 JSON content-type로 발행되는지
  검증한다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.api.ingest_completion import IngestCompletionEvent, QueueIngestCompletionPublisher
from app.ingestion.workers.publisher import PikaQueuePublisher
from app.schemas.enums import IngestJobStatus


class _FakePikaChannel:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def basic_publish(
        self, *, exchange: str, routing_key: str, body: bytes, properties: object
    ) -> None:
        self.calls.append(
            {
                "exchange": exchange,
                "routing_key": routing_key,
                "body": body,
                "properties": properties,
            }
        )


def test_completion_event_rejects_missing_job_id() -> None:
    with pytest.raises(ValueError, match="job_id is required"):
        IngestCompletionEvent(
            job_id="",
            mode="full",
            admin_user_id="admin-1",
            status=IngestJobStatus.COMPLETED,
        )


def test_completion_event_rejects_missing_admin_user_id() -> None:
    with pytest.raises(ValueError, match="admin_user_id is required"):
        IngestCompletionEvent(
            job_id="job-1",
            mode="full",
            status=IngestJobStatus.COMPLETED,
            admin_user_id="",
        )


def test_completion_event_rejects_non_terminal_status() -> None:
    with pytest.raises(ValueError, match="status must be COMPLETED or FAILED"):
        IngestCompletionEvent(
            job_id="job-1",
            mode="full",
            admin_user_id="admin-1",
            status=IngestJobStatus.STARTED,  # type: ignore[arg-type]
        )


def test_completion_publish_sets_persistent_delivery_mode_and_json_content_type() -> None:
    channel = _FakePikaChannel()
    publisher = QueueIngestCompletionPublisher(
        PikaQueuePublisher(channel),
        routing_key="lina.admin.ingest.completion",
    )

    publisher.publish(
        IngestCompletionEvent(
            job_id="job-1",
            mode="full",
            admin_user_id="admin-1",
            status=IngestJobStatus.COMPLETED,
        )
    )

    published = channel.calls[0]
    properties = published["properties"]
    assert published["exchange"] == ""
    assert published["routing_key"] == "lina.admin.ingest.completion"
    assert getattr(properties, "delivery_mode", None) == 2
    assert getattr(properties, "content_type", None) == "application/json"

    payload = json.loads(published["body"].decode("utf-8"))
    assert payload["jobId"] == "job-1"
    assert payload["adminUserId"] == "admin-1"
