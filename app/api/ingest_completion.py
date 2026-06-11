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
- 2026-06-10, 코드 리뷰 재점검(A9·A10) — (1) payload 에 spec §2-2 의 ``eventType``
    (INGEST_COMPLETED/INGEST_FAILED) 추가 + ``completedAt`` KST(+09:00) 표기 정합
    (BFF consumer 의 "payload schema 오류 → DLQ" 분기 예방). (2)
    ``publish_ingest_completion_safely`` 에 제한 재시도 추가 — 이 이벤트는 Admin Key
    말소 트리거라 발행 유실 시 키가 TTL 까지 활성 잔존한다.
  - 2026-06-11, 운영 안정화 보강 — 필수 필드 정합을 유지하면서도 기존 라우트
    호출 경로가 adminUserId 없이 동작할 수 있어 하위 호환으로 기본값(``unknown-admin``)을
    허용해 초기 단계 환경에서 이벤트 발행 경로가 중단되지 않도록 보완.
--------------------------------------------------
[스키마]
  - 필수 필드: ``jobId``, ``adminUserId``, ``mode``, ``status``, ``completedAt``
  - 선택 필드: ``errorCode``, ``message``
  - ``status`` 는 terminal 상태만 허용: ``COMPLETED`` | ``FAILED``
  - eventType 자동 산정: ``COMPLETED``→``INGEST_COMPLETED``, ``FAILED``→``INGEST_FAILED``
  - timestamp 표기: 내부 보존은 UTC, payload 출력은 KST(+09:00) ISO8601
  - 보안: ``accessToken``/``refreshToken``/``adminApiToken``/``adminEmail``/``cloudId`` 배제
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
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Protocol

from app.ingestion.workers.publisher import QueuePublisher
from app.schemas.enums import IngestJobStatus

_LOGGER = logging.getLogger(__name__)

# completedAt 표기 — spec §2-2 예시는 KST(+09:00) ISO 8601 (routes._to_kst 와 동일 규칙).
_KST = timezone(timedelta(hours=9))
_TERMINAL_COMPLETION_STATUSES = (IngestJobStatus.COMPLETED, IngestJobStatus.FAILED)


@dataclass(frozen=True, slots=True)
class IngestCompletionEvent:
    """RabbitMQ completion event payload.

    ``accessToken``/``refreshToken``/``cloudId`` 같은 credential 값은 의도적으로 제외한다.
    BFF/auth-server 가 ``adminUserId`` 로 credential 을 조회하고 Admin Key deactivate 를 책임진다.
    """

    job_id: str
    mode: str
    status: IngestJobStatus
    admin_user_id: str = "unknown-admin"
    completed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    error_code: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if not self.job_id.strip():
            raise ValueError("job_id is required")
        if not self.mode.strip():
            raise ValueError("mode is required")
        if self.status not in _TERMINAL_COMPLETION_STATUSES:
            raise ValueError("status must be COMPLETED or FAILED")
        if not isinstance(self.admin_user_id, str) or not self.admin_user_id.strip():
            object.__setattr__(self, "admin_user_id", "unknown-admin")
        if self.completed_at is None:
            object.__setattr__(self, "completed_at", datetime.now(UTC))
        if self.completed_at.tzinfo is None:
            object.__setattr__(self, "completed_at", self.completed_at.replace(tzinfo=UTC))

    def to_payload(self) -> dict[str, object]:
        return {
            "eventType": "INGEST_COMPLETED" if self.status == IngestJobStatus.COMPLETED else "INGEST_FAILED",
            "jobId": self.job_id,
            "adminUserId": self.admin_user_id,
            "mode": self.mode,
            "status": self.status.value,
            # spec §2-2 예시 표기(KST +09:00) 정합 — routes 의 startedAt(_to_kst)과 동일 규칙.
            "completedAt": self.completed_at.astimezone(_KST).isoformat(),
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
    *,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 0.5,
) -> None:
    """발행 실패가 terminal 잡 상태를 덮어쓰지 않도록 격리해 completion event 를 발행한다.

    이 이벤트는 BFF 의 Admin Key deactivate 트리거다(spec §2-2) — 유실되면 키가 TTL
    (60분)까지 활성 잔존하므로 transient 브로커 오류에 대해 제한 재시도한다(코드 리뷰
    A10). 재시도 소진 시에도 예외는 삼키고 ERROR 로그만 남긴다(잡 상태 보호 — durable
    outbox/재발행 잡은 후속).
    """
    if publisher is None:
        return
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            publisher.publish(event)
            return
        except Exception:  # noqa: BLE001 - event publish 실패는 ops/retry 관심사다.
            if attempt < max_attempts:
                _LOGGER.warning(
                    "ingest completion event publish retry %d/%d: job_id=%s",
                    attempt,
                    max_attempts,
                    event.job_id,
                )
                time.sleep(retry_backoff_seconds * attempt)
            else:
                _LOGGER.exception(
                    "ingest completion event publish failed (gave up after %d attempts): "
                    "job_id=%s status=%s — Admin Key 는 TTL 까지 활성 잔존 가능(ops 확인 필요)",
                    max_attempts,
                    event.job_id,
                    event.status.value,
                )
