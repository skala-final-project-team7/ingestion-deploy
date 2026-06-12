"""수집 완료 이벤트 단위 테스트 (api-spec v2.5.0).

작성자 : 최태성
담당 영역 : ingestion

- completion event payload 에 Confluence credential(accessToken/refreshToken/cloudId)이
  포함되지 않는지 검증한다.
- payload 의 spec §2-2 정합(A9) — ``completedAt`` KST(+09:00) 표기를 검증한다.
- ``QueueIngestCompletionPublisher`` 가 routing key + payload 를 ``QueuePublisher`` 에
  전달하는지 검증한다.
- Noop publisher 와 publisher 실패 격리 + 제한 재시도(A10 — transient 실패 후 성공 시
  즉시 반환)를 검증한다.
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
        # spec §2-2 표기 정합(A9) — UTC 입력도 KST(+09:00)로 변환해 발행한다.
        "completedAt": "2026-06-05T10:02:03+09:00",
        "errorCode": None,
        "message": None,
    }
    assert "accessToken" not in payload
    assert "refreshToken" not in payload
    assert "cloudId" not in payload


def test_completion_event_payload_follows_terminal_status() -> None:
    """A9 — status 값이 완료/실패 일치 여부를 보존한다."""
    completed = IngestCompletionEvent(
        job_id="job-1",
        admin_user_id="712020:admin",
        mode="full",
        status=IngestJobStatus.COMPLETED,
    )
    failed = IngestCompletionEvent(
        job_id="job-2",
        admin_user_id="712020:admin",
        mode="delta",
        status=IngestJobStatus.FAILED,
        error_code="INGEST_FAILED",
    )

    assert completed.to_payload()["status"] == "COMPLETED"
    assert failed.to_payload()["status"] == "FAILED"


def test_completion_event_payload_completed_at_is_kst_isoformat() -> None:
    """A9 — completedAt 은 spec §2-2 예시 표기(KST +09:00) ISO 8601 이다(naive 입력도 UTC 간주)."""
    explicit = IngestCompletionEvent(
        job_id="job-1",
        admin_user_id="712020:admin",
        mode="full",
        status=IngestJobStatus.COMPLETED,
        completed_at=datetime(2026, 6, 5, 23, 30, 0, tzinfo=UTC),
    )
    assert explicit.to_payload()["completedAt"] == "2026-06-06T08:30:00+09:00"

    # naive 입력은 UTC 로 간주된 뒤 KST 로 변환된다.
    naive = IngestCompletionEvent(
        job_id="job-naive",
        admin_user_id="712020:admin",
        mode="full",
        status=IngestJobStatus.COMPLETED,
        completed_at=datetime(2026, 6, 5, 1, 2, 3),
    )
    assert naive.to_payload()["completedAt"] == "2026-06-05T10:02:03+09:00"

    # completed_at 미지정(now 폴백)도 항상 +09:00 오프셋으로 직렬화된다.
    fallback = IngestCompletionEvent(
        job_id="job-2",
        admin_user_id="712020:admin",
        mode="full",
        status=IngestJobStatus.COMPLETED,
    )
    assert str(fallback.to_payload()["completedAt"]).endswith("+09:00")


def test_queue_completion_publisher_uses_routing_key() -> None:
    queue = FakeQueuePublisher()
    publisher = QueueIngestCompletionPublisher(queue, routing_key="lina.admin.ingest.completion")

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
    assert published.routing_key == "lina.admin.ingest.completion"
    assert published.body["jobId"] == "job-1"
    assert published.body["adminUserId"] == "712020:admin"
    assert published.body["status"] == "FAILED"
    assert published.body["errorCode"] == "INGEST_FAILED"
    assert published.body["completedAt"] == "2026-06-05T10:02:03+09:00"
    assert "cloudId" not in published.body


def test_noop_completion_publisher_is_safe() -> None:
    publish_ingest_completion_safely(
        NoopIngestCompletionPublisher(),
        IngestCompletionEvent(
            job_id="job-1",
            admin_user_id="712020:admin",
            mode="full",
            status=IngestJobStatus.COMPLETED,
        ),
    )


def test_publish_safely_swallows_publisher_errors() -> None:
    class FailingPublisher:
        def __init__(self) -> None:
            self.attempts = 0

        def publish(self, event: IngestCompletionEvent) -> None:
            self.attempts += 1
            raise RuntimeError("rabbitmq unavailable")

    publisher = FailingPublisher()
    # 재시도 소진(A10) 후에도 예외를 삼킨다(terminal 잡 상태 보호). backoff=0 으로 즉시 검증.
    publish_ingest_completion_safely(
        publisher,
        IngestCompletionEvent(
            job_id="job-1",
            admin_user_id="712020:admin",
            mode="full",
            status=IngestJobStatus.COMPLETED,
        ),
        retry_backoff_seconds=0,
    )

    assert publisher.attempts == 3  # 기본 max_attempts 만큼 시도 후 포기


def test_publish_safely_retries_transient_failure_then_succeeds() -> None:
    """A10 — 1회 transient 실패 후 성공하면 재시도에서 발행되고 추가 시도 없이 반환한다."""

    class FlakyPublisher:
        def __init__(self) -> None:
            self.attempts = 0
            self.published: list[IngestCompletionEvent] = []

        def publish(self, event: IngestCompletionEvent) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("rabbitmq hiccup")
            self.published.append(event)

    publisher = FlakyPublisher()
    event = IngestCompletionEvent(
        job_id="job-1",
        admin_user_id="712020:admin",
        mode="full",
        status=IngestJobStatus.COMPLETED,
    )

    publish_ingest_completion_safely(publisher, event, retry_backoff_seconds=0)

    assert publisher.attempts == 2  # 실패 1 + 성공 1 — 성공 즉시 반환(3회까지 안 감)
    assert publisher.published == [event]
