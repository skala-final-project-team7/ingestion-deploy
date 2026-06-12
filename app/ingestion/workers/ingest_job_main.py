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

from __future__ import annotations

import logging
import signal
import time
from types import FrameType

from app.api.deps import build_ingest_deps
from app.config import Settings, get_settings
from app.ingestion.bootstrap import open_rabbitmq_channel
from app.ingestion.workers.consumer import PikaMessageConsumer
from app.ingestion.workers.ingestion_worker import run_ingest_job_from_payload

_LOGGER = logging.getLogger(__name__)

_RETRY_BACKOFF_SECONDS = (1, 2, 5, 10, 30)
_shutdown_requested = False


def _request_shutdown(signum: int, _frame: FrameType | None) -> None:
    """시그널 기반 종료 플래그와 KeyboardInterrupt 주입."""
    global _shutdown_requested  # noqa: PLW0603
    _shutdown_requested = True
    raise KeyboardInterrupt


def _consume_until_shutdown(settings: Settings) -> None:
    """RabbitMQ 연결 1회 수명으로 ingest job 큐를 소비한다."""
    deps = build_ingest_deps(settings)
    connection, channel = open_rabbitmq_channel(settings)
    try:
        channel.basic_qos(prefetch_count=1)
        consumer = PikaMessageConsumer(channel, queue=settings.ingest_job_queue)
        _LOGGER.info("RabbitMQ 연결 완료 — ingest job 소비 시작: queue=%s", settings.ingest_job_queue)
        for message in consumer.consume():
            try:
                run_ingest_job_from_payload(
                    deps,
                    message,
                    credential_lookup=getattr(deps, "credential_lookup", None),
                )
            except Exception:
                _LOGGER.exception("ingest job consume 실패: message_keys=%s", sorted(message.keys()))
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
