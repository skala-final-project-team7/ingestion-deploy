"""completion 이벤트 통합 시나리오 (Testcontainers 가능 시).

실제 RabbitMQ broker 가 구동 가능한 환경에서 completion queue에 대한
발행/소비 경로를 검증한다.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pytest

from app.api.ingest_completion import IngestCompletionEvent, QueueIngestCompletionPublisher
from app.config import Settings
from app.ingestion.workers.publisher import PikaQueuePublisher
from app.schemas.enums import IngestJobStatus

pytestmark = pytest.mark.integration


try:
    import pika
except Exception:  # pragma: no cover - optional dependency/환경
    pika = None


try:
    from testcontainers.rabbitmq import RabbitMqContainer
except Exception:  # pragma: no cover - optional dependency/환경
    RabbitMqContainer = None


def _resolve_rabbitmq_url(container: object) -> str:
    if hasattr(container, "get_connection_url"):
        url = container.get_connection_url()  # type: ignore[union-attr]
        if isinstance(url, str) and url.startswith("amqp://"):
            return url

    if hasattr(container, "get_connection_url_with_credentials"):
        url = container.get_connection_url_with_credentials()  # type: ignore[union-attr]
        if isinstance(url, str) and url.startswith("amqp://"):
            return url

    if hasattr(container, "get_container_host_ip") and hasattr(container, "get_exposed_port"):
        host = container.get_container_host_ip()  # type: ignore[union-attr]
        port = container.get_exposed_port(5672)  # type: ignore[union-attr]
        return f"amqp://guest:guest@{host}:{port}/"

    raise RuntimeError("Testcontainers container 인터페이스에서 RabbitMQ URL 추출 불가")


def _consume_with_wait(channel, queue: str) -> tuple[object, object, object]:
    for _ in range(40):
        method, properties, body = channel.basic_get(queue=queue, auto_ack=True)
        if method is not None:
            return method, properties, body
        time.sleep(0.1)
    raise AssertionError("completion queue에서 이벤트 수신 실패")


def test_completion_queue_receive_and_consume_with_rabbitmq_container() -> None:
    if RabbitMqContainer is None or pika is None:
        pytest.skip("testcontainers 또는 pika 미설치 환경에서는 통합 테스트를 실행할 수 없음")

    settings = Settings(
        rabbitmq_url="amqp://guest:guest@localhost:5672/%2F",
        ingest_completion_queue="lina.admin.ingest.completion",
        ingest_completion_routing_key="lina.admin.ingest.completion",
    )

    try:
        with RabbitMqContainer() as container:
            settings = Settings(
                rabbitmq_url=_resolve_rabbitmq_url(container),
                ingest_completion_queue="lina.admin.ingest.completion",
                ingest_completion_routing_key="lina.admin.ingest.completion",
                ingest_completion_dlq="lina.admin.ingest.completion.dlq",
            )
            connection = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq_url))
            try:
                channel = connection.channel()
                channel.queue_declare(queue=settings.ingest_completion_queue, durable=True)
                channel.queue_declare(queue=settings.ingest_completion_dlq, durable=True)

                publisher = QueueIngestCompletionPublisher(
                    PikaQueuePublisher(channel),
                    routing_key=settings.ingest_completion_routing_key,
                )

                completed = IngestCompletionEvent(
                    job_id="job-complete-1",
                    mode="full",
                    status=IngestJobStatus.COMPLETED,
                    admin_user_id="admin-42",
                    completed_at=datetime(2026, 6, 11, 9, 0, 0, tzinfo=UTC),
                )
                failed = IngestCompletionEvent(
                    job_id="job-failed-1",
                    mode="delta",
                    status=IngestJobStatus.FAILED,
                    admin_user_id="admin-42",
                    error_code="INGEST_FAILED",
                    message="test integration failure",
                    completed_at=datetime(2026, 6, 11, 9, 0, 1, tzinfo=UTC),
                )

                publisher.publish(completed)
                publisher.publish(failed)

                method_1, props_1, body_1 = _consume_with_wait(
                    channel, settings.ingest_completion_queue
                )
                method_2, props_2, body_2 = _consume_with_wait(
                    channel, settings.ingest_completion_queue
                )

                assert getattr(props_1, "delivery_mode", None) == 2
                assert getattr(props_1, "content_type", None) == "application/json"
                assert getattr(props_2, "delivery_mode", None) == 2
                assert getattr(props_2, "content_type", None) == "application/json"

                payload_1 = json.loads(body_1.decode("utf-8"))
                payload_2 = json.loads(body_2.decode("utf-8"))

                assert payload_1["status"] == "COMPLETED"
                assert payload_1["jobId"] == "job-complete-1"
                assert "accessToken" not in payload_1
                assert "refreshToken" not in payload_1
                assert "cloudId" not in payload_1

                assert payload_2["status"] == "FAILED"
                assert payload_2["jobId"] == "job-failed-1"
                assert payload_2["errorCode"] == "INGEST_FAILED"
                assert "test integration failure" in str(payload_2["message"])

                assert method_1 is not None
                assert method_2 is not None
            finally:
                connection.close()
    except Exception as exc:
        pytest.skip(f"rabbitmq container 실행 불가: {exc}")
