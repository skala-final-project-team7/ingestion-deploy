"""수집 완료 이벤트 단위 테스트 (api-spec v2.5.0).

- completion event payload 에 Confluence credential(accessToken/refreshToken/cloudId)이
  포함되지 않는지 검증한다.
- ``QueueIngestCompletionPublisher`` 가 routing key + payload 를 ``QueuePublisher`` 에
  전달하는지 검증한다.
- Noop publisher 와 publisher 실패 격리 동작을 검증한다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.api.ingest_completion import (
    IngestCompletionEvent,
    NoopIngestCompletionPublisher,
    QueueIngestCompletionPublisher,
    publish_ingest_completion_safely,
)
from app.ingestion.workers.publisher import FakeQueuePublisher
from app.schemas.enums import IngestJobStatus


def test_completion_event_payload_excludes_confluence_credentials() -> None:
    event = IngestCompletionEvent(
        job_id="job-1",
        admin_user_id="712020:admin",
        mode="full",
        status=IngestJobStatus.COMPLETED,
        completed_at=datetime(2026, 6, 5, 1, 2, 3, tzinfo=UTC),
    )

    payload = event.to_payload()

    assert payload == {
        "jobId": "job-1",
        "adminUserId": "712020:admin",
        "mode": "full",
        "status": "COMPLETED",
        "completedAt": "2026-06-05T01:02:03+00:00",
        "errorCode": None,
        "message": None,
    }
    assert "accessToken" not in payload
    assert "refreshToken" not in payload
    assert "cloudId" not in payload


def test_queue_completion_publisher_uses_routing_key() -> None:
    queue = FakeQueuePublisher()
    publisher = QueueIngestCompletionPublisher(queue, routing_key="ingestion.completed")

    publisher.publish(
        IngestCompletionEvent(
            job_id="job-1",
            admin_user_id="712020:admin",
            mode="delta",
            status=IngestJobStatus.FAILED,
            error_code="INGEST_FAILED",
            message="boom",
            completed_at=datetime(2026, 6, 5, 1, 2, 3, tzinfo=UTC),
        )
    )

    assert len(queue.messages) == 1
    published = queue.messages[0]
    assert published.routing_key == "ingestion.completed"
    assert published.body["jobId"] == "job-1"
    assert published.body["adminUserId"] == "712020:admin"
    assert published.body["status"] == "FAILED"
    assert published.body["errorCode"] == "INGEST_FAILED"
    assert "cloudId" not in published.body


def test_noop_completion_publisher_is_safe() -> None:
    publish_ingest_completion_safely(
        NoopIngestCompletionPublisher(),
        IngestCompletionEvent(
            job_id="job-1",
            mode="full",
            status=IngestJobStatus.COMPLETED,
        ),
    )


def test_publish_safely_swallows_publisher_errors() -> None:
    class FailingPublisher:
        def publish(self, event: IngestCompletionEvent) -> None:
            raise RuntimeError("rabbitmq unavailable")

    publish_ingest_completion_safely(
        FailingPublisher(),
        IngestCompletionEvent(
            job_id="job-1",
            mode="full",
            status=IngestJobStatus.COMPLETED,
        ),
    )
