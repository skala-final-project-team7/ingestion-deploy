"""Ingestion Job Consumer 실행 진입점 — BFF ingest request consume [Pipeline].

--------------------------------------------------
작성자 : 이다연
작성목적 : BFF가 exchange ``lina.admin.ingest``(``admin.ingest.requested``)로 발행한
          ingest job 메시지를 소비해 수집 job(full 또는 delta) 실행 루프를 기동한다.
          본 모듈은 RabbitMQ 연결/consume loop를 소유하며, 메시지 파싱·디스패치는
          ``app.ingestion.workers.ingestion_worker`` 에 위임한다.

          실행:  python -m app.ingestion.workers.ingest_job_main
--------------------------------------------------
"""

import json
import logging
import signal
import time
from collections.abc import Callable, Mapping
from types import FrameType
from typing import Any

from app.api.deps import build_ingest_deps
from app.config import Settings, get_settings
from app.ingestion.bootstrap import open_rabbitmq_channel
from app.ingestion.workers.ingestion_worker import run_ingest_job_from_payload

_LOGGER = logging.getLogger(__name__)

_RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 10.0)
_MAX_CONSUME_ATTEMPTS = len(_RETRY_BACKOFF_SECONDS)
_RETRY_HEADER_NAME = "x-retry-count"
_shutdown_requested = False


CredentialLookup = Callable[[str], tuple[str | None, str | None]]


def _resolve_credential_lookup(deps: Any) -> CredentialLookup | None:
    """deps 에서 credential_lookup callable 을 추출한다."""
    candidate = getattr(deps, "credential_lookup", None)
    return candidate if callable(candidate) else None


def _request_shutdown(signum: int, _frame: FrameType | None) -> None:
    """시그널 기반 종료 플래그와 KeyboardInterrupt 주입."""
    global _shutdown_requested  # noqa: PLW0603
    _shutdown_requested = True
    raise KeyboardInterrupt


def _get_retry_count(headers: Any) -> int:
    """메시지 헤더의 `x-retry-count`를 정수로 파싱한다(없으면 0)."""
    try:
        if headers is None:
            return 0
        value = headers.get(_RETRY_HEADER_NAME, 0)
        count = int(value)
        if count < 0:
            return 0
        return count
    except (TypeError, ValueError):
        _LOGGER.warning("retry count 파싱 실패: header=%r. 0부터 시작합니다.", headers)
        return 0


def _publish_retry_message(
    channel: Any,
    queue: str,
    body: bytes,
    properties: Any,
    *,
    retry_count: int,
) -> None:
    """메시지 retry 카운트를 갱신해 동일 큐에 재발행한다."""
    import pika

    headers = {}
    original_headers = getattr(properties, "headers", None)
    if isinstance(original_headers, dict):
        headers.update(original_headers)
    headers[_RETRY_HEADER_NAME] = retry_count
    retry_properties = pika.BasicProperties(
        content_type=getattr(properties, "content_type", None),
        delivery_mode=getattr(properties, "delivery_mode", 2),
        content_encoding=getattr(properties, "content_encoding", None),
        correlation_id=getattr(properties, "correlation_id", None),
        reply_to=getattr(properties, "reply_to", None),
        headers=headers,
    )
    channel.basic_publish(exchange="", routing_key=queue, body=body, properties=retry_properties)


def _handle_failure(
    channel: Any,
    *,
    queue: str,
    method: Any,
    body: bytes,
    properties: Any,
    error: Exception,
) -> None:
    """실패 메시지를 재시도/최종 DLQ 정책으로 처리한다."""
    retry_count = _get_retry_count(getattr(properties, "headers", None))

    if retry_count < _MAX_CONSUME_ATTEMPTS - 1:
        _LOGGER.warning(
            "ingest job consume 실패 후 재시도 대기: job_message=%r retry_count=%d error=%s",
            method.delivery_tag,
            retry_count,
            error,
        )
        delay = _RETRY_BACKOFF_SECONDS[retry_count]
        time.sleep(delay)
        next_retry_count = retry_count + 1
        try:
            _publish_retry_message(
                channel,
                queue,
                body,
                properties,
                retry_count=next_retry_count,
            )
            channel.basic_ack(method.delivery_tag)
            return
        except Exception as publish_error:
            _LOGGER.exception(
                "ingest job retry publish 실패 — DLQ로 이동: "
                "queue=%s retry_count=%d publish_error=%r",
                queue,
                retry_count,
                publish_error,
            )
            channel.basic_reject(method.delivery_tag, requeue=False)
            return

    _LOGGER.error(
        "ingest job 최종 실패: retry_count=%d — DLQ로 이동 처리. delivery_tag=%s",
        retry_count,
        method.delivery_tag,
    )
    channel.basic_reject(method.delivery_tag, requeue=False)


def _parse_message(body: bytes) -> Mapping[str, object]:
    """메시지를 JSON dict로 변환한다. 실패 시 상위에서 재시도 정책으로 처리."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed message body") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("message body must be dict")
    return payload


def _consume_until_shutdown(settings: Settings) -> None:
    """RabbitMQ 연결 1회 수명으로 ingest job 큐를 소비한다."""
    deps = build_ingest_deps(settings)
    credential_lookup = _resolve_credential_lookup(deps)
    connection, channel = open_rabbitmq_channel(settings)
    try:
        channel.basic_qos(prefetch_count=1)
        _LOGGER.info(
            "RabbitMQ 연결 완료 — ingest job 소비 시작: queue=%s", settings.ingest_job_queue
        )
        for method, properties, body in channel.consume(settings.ingest_job_queue, auto_ack=False):
            try:
                message = _parse_message(body)
                run_ingest_job_from_payload(
                    deps,
                    message,
                    credential_lookup=credential_lookup,
                )
                channel.basic_ack(method.delivery_tag)
            except Exception as exc:
                _LOGGER.exception(
                    "ingest job consume 실패: delivery_tag=%s",
                    getattr(method, "delivery_tag", None),
                )
                _handle_failure(
                    channel,
                    queue=settings.ingest_job_queue,
                    method=method,
                    body=body,
                    properties=properties,
                    error=exc,
                )
            if _shutdown_requested:
                break
    finally:
        connection.close()


def main() -> None:
    """워커 엔트리포인트."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    settings = get_settings()
    import pika.exceptions

    attempt = 0
    while not _shutdown_requested:
        try:
            _consume_until_shutdown(settings)
            attempt = 0
        except KeyboardInterrupt:
            break
        except (
            pika.exceptions.AMQPConnectionError,
            pika.exceptions.StreamLostError,
            pika.exceptions.ChannelClosedByBroker,
        ) as exc:
            if _shutdown_requested:
                break
            delay = _RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)]
            attempt += 1
            _LOGGER.warning(
                "RabbitMQ 연결 단절(%s: %s) — %s초 후 재연결(시도 %s)",
                type(exc).__name__,
                exc,
                delay,
                attempt,
            )
            time.sleep(delay)


if __name__ == "__main__":
    main()
