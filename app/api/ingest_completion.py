"""수집 완료 이벤트 발행 — RabbitMQ completion event [Pipeline 경계].

--------------------------------------------------
작성자 : 최태성
작성목적 : api-spec v2.5.0 은 기존 BFF polling / ML→BFF HTTP revoke callback 흐름을 RabbitMQ
          completion event 로 대체한다. 수집 잡이 terminal(COMPLETED/FAILED) 상태에 도달하면
          ML/Data Ingestion 이 completion event 를 발행하고, BFF consumer 가 이를 consume 해
          auth-server 의 Admin Key deactivate 내부 API 를 호출한다. ML 은 Atlassian Admin Key 를
          직접 말소하지 않는다(책임 분리). event payload 에는 ``accessToken``/``refreshToken``/
          ``cloudId`` 같은 credential set 을 절대 싣지 않는다(루트 CLAUDE.md 보안 규칙).
작성일 : 2026-06-09 (api-spec v2.5.0 §2-2 수집 완료 이벤트)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-06-09, api-spec v2.5.0 정합 — IngestCompletionEvent + publisher seam(Noop/Queue)
    추가. credential 없는 completion event payload 계약을 고정한다.
--------------------------------------------------
[보안] payload 는 jobId/adminUserId/mode/status/completedAt/errorCode/message 만 포함한다.
       credential 값(accessToken/refreshToken/cloudId)은 의도적으로 제외한다.
[호환성]
  - Python 3.11.x
  - 외부 의존성 0 (QueuePublisher 추상화에만 의존 — pika 미설치 환경에서도 Noop 동작)
--------------------------------------------------
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.ingestion.workers.publisher import QueuePublisher
from app.schemas.enums import IngestJobStatus

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IngestCompletionEvent:
    """RabbitMQ completion event payload.

    ``accessToken``/``refreshToken``/``cloudId`` 같은 credential 값은 의도적으로 제외한다.
    BFF/auth-server 가 ``adminUserId`` 로 credential 을 조회하고 Admin Key deactivate 를 책임진다.
    """

    job_id: str
    mode: str
    status: IngestJobStatus
    admin_user_id: str | None = None
    error_code: str | None = None
    message: str | None = None
    completed_at: datetime | None = None

    def to_payload(self) -> dict[str, object]:
        completed_at = self.completed_at or datetime.now(UTC)
        return {
            "jobId": self.job_id,
            "adminUserId": self.admin_user_id,
            "mode": self.mode,
            "status": self.status.value,
            "completedAt": completed_at.isoformat(),
            "errorCode": self.error_code,
            "message": self.message,
        }


class IngestCompletionPublisher(Protocol):
    """Completion event publisher seam."""

    def publish(self, event: IngestCompletionEvent) -> None:
        """terminal 수집 이벤트를 발행하거나 기록한다."""


@dataclass(frozen=True, slots=True)
class NoopIngestCompletionPublisher:
    """No-op publisher — local HTTP smoke·테스트 기본값(명시 주입 전까지)."""

    def publish(self, event: IngestCompletionEvent) -> None:
        return None


@dataclass(frozen=True, slots=True)
class QueueIngestCompletionPublisher:
    """``QueuePublisher`` 기반 completion event publisher."""

    publisher: QueuePublisher
    routing_key: str = "ingestion.completed"

    def publish(self, event: IngestCompletionEvent) -> None:
        self.publisher.publish(routing_key=self.routing_key, message=event.to_payload())


def publish_ingest_completion_safely(
    publisher: IngestCompletionPublisher | None,
    event: IngestCompletionEvent,
) -> None:
    """발행 실패가 terminal 잡 상태를 덮어쓰지 않도록 격리해 completion event 를 발행한다."""
    if publisher is None:
        return
    try:
        publisher.publish(event)
    except Exception:  # noqa: BLE001 - event publish 실패는 ops/retry 관심사다.
        _LOGGER.exception(
            "ingest completion event publish failed: job_id=%s status=%s",
            event.job_id,
            event.status.value,
        )
