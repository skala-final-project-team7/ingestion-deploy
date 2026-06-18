"""Chunking+Embedding Worker 실행 진입점 — content.chunking 장기 소비 loop [Pipeline].

--------------------------------------------------
작성자 : 최태성
담당 영역 : ingestion
작성목적 : 배포 전 점검(2026-06-11)에서 확인된 "워커 실행 진입점 부재" 해소. 종전에는
          bootstrap(build_chunking_worker_deps)·PikaMessageConsumer 등 부품만 있고 이를
          실행하는 프로세스 진입점이 없어, infra 가 자체 코드를 작성하기 전까지 운영
          색인 경로가 존재하지 않았다. 본 모듈이 RabbitMQ 연결을 소유하고
          ``content.chunking`` 큐를 소비해 raw_pages/raw_attachments → 청킹 → 임베딩 →
          Qdrant upsert 전 체인을 구동한다.

          실행:  python -m app.ingestion.workers.chunking_main
          또는:  ingestion-chunking-worker  (pyproject [project.scripts])
작성일 : 2026-06-11
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-06-11, 최초 작성, 배포 전 점검 fix — 연결 재시도 backoff·SIGTERM 우아 종료·
    prefetch=1 공정 분배·기동 모드 로깅(PoC 모드 경고) 포함.
--------------------------------------------------
[호환성]
  - Python 3.11.x, pika>=1.3
  - 운영(use_real_adapters=True)은 embedding extra(sentence-transformers/fastembed) +
    Qdrant/MongoDB(/MySQL·OpenAI — 문서 분석기) 접속을 요구한다.
  - PoC(False)로도 뜨지만 전부 Fake store 라 색인 결과가 남지 않는다(경고 로그).
--------------------------------------------------
"""

from __future__ import annotations

import logging
import os
import signal
import time
from types import FrameType

from prometheus_client import start_http_server

from app.config import Settings, get_settings
from app.ingestion.bootstrap import build_chunking_worker_deps
from app.ingestion.workers import QUEUE_CHUNKING
from app.ingestion.workers.chunking_worker import iter_chunking_worker
from app.ingestion.workers.consumer import PikaMessageConsumer

_LOGGER = logging.getLogger(__name__)

# 연결 실패 재시도 backoff(초) — 마지막 값으로 포화. RabbitMQ 가 늦게 떠도 워커가
# crash-loop 대신 자체 재시도한다(k8s 재시작 정책과 중복돼도 무해).
_RETRY_BACKOFF_SECONDS = (1, 2, 5, 10, 30)
_DEFAULT_METRICS_PORT = 8002

# SIGTERM(k8s 종료)·SIGINT 공용 종료 플래그 — consume loop 가 다음 메시지 경계에서 멈춘다.
_shutdown_requested = False


def _request_shutdown(signum: int, _frame: FrameType | None) -> None:
    """종료 플래그 set + KeyboardInterrupt — 블로킹 consume 을 즉시 깨운다.

    플래그만으로는 다음 메시지가 올 때까지 blocking ``channel.consume`` 이 깨어나지
    않으므로, 메인 스레드 시그널 핸들러에서 KeyboardInterrupt 를 올려 select 대기를
    중단시킨다. 미ack 메시지는 connection.close() 시 브로커가 재전송한다(at-least-once).
    """
    global _shutdown_requested  # noqa: PLW0603 — 시그널 핸들러의 단순 플래그 set.
    _shutdown_requested = True
    _LOGGER.info("종료 시그널 수신(%s) — 우아 종료를 시작한다", signal.Signals(signum).name)
    raise KeyboardInterrupt


def _log_boot_mode(settings: Settings) -> None:
    """기동 시 PoC/운영 모드를 명시 로그로 남긴다(무음 PoC 부팅 방지 — 배포 전 점검)."""
    if settings.use_real_adapters:
        _LOGGER.info(
            "chunking worker 기동 — 운영(real) 모드: queue=%s qdrant=%s:%s mongo_db=%s "
            "dense_model=%s",
            QUEUE_CHUNKING,
            settings.qdrant_host,
            settings.qdrant_port,
            settings.mongo_db,
            settings.dense_embedding_model,
        )
    else:
        _LOGGER.warning(
            "chunking worker 기동 — PoC 모드(전부 Fake store): 소비한 메시지가 실 Qdrant 에 "
            "적재되지 않는다. 운영 배포라면 RAG_USE_REAL_ADAPTERS=true 가 필요하다"
        )


def _consume_until_shutdown(settings: Settings) -> None:
    """RabbitMQ 연결 1회 수명 — 큐 선언(durable)·prefetch=1·소비 loop.

    연결이 살아 있는 동안 블로킹한다. 연결 단절 예외는 호출자(main 의 재시도 loop)가
    처리한다. ack 는 ``PikaMessageConsumer`` 가 메시지 처리 완료 후 수행한다
    (at-least-once — 미처리 종료 시 브로커가 재전송).
    """
    import pika

    deps = build_chunking_worker_deps(settings)
    connection = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq_url))
    try:
        channel = connection.channel()
        channel.queue_declare(queue=QUEUE_CHUNKING, durable=True)
        # 공정 분배 — 다중 replica 에서 미ack 1건 초과 선취를 막는다(긴 페이지 처리 보호).
        channel.basic_qos(prefetch_count=1)
        consumer = PikaMessageConsumer(channel, queue=QUEUE_CHUNKING)
        _LOGGER.info("RabbitMQ 연결 완료 — %s 소비 시작", QUEUE_CHUNKING)
        for result in iter_chunking_worker(consumer, deps):
            _LOGGER.info(
                "색인 1건 완료 — page_id=%s attachment_id=%s status=%s chunks=%s upserted=%s "
                "skipped=%s",
                result.page_id,
                result.attachment_id,
                result.status.value,
                result.chunks,
                result.upserted,
                result.skipped,
            )
            if _shutdown_requested:
                break
    finally:
        # close() 가 미ack 메시지를 브로커로 되돌린다(재전송) — 우아 종료.
        connection.close()


def main() -> None:
    """워커 프로세스 진입점 — 시그널 등록 + 연결 재시도 loop."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    metrics_port = int(os.environ.get("RAG_WORKER_METRICS_PORT", _DEFAULT_METRICS_PORT))
    start_http_server(metrics_port)
    _LOGGER.info("Prometheus metrics 서버 기동 — port=%s path=/metrics", metrics_port)

    settings = get_settings()
    _log_boot_mode(settings)

    import pika.exceptions

    attempt = 0
    while not _shutdown_requested:
        try:
            _consume_until_shutdown(settings)
            attempt = 0  # 셧다운 요청에 의한 정상 복귀 — backoff 리셋.
        except KeyboardInterrupt:
            # 시그널 핸들러가 올린 우아 종료 — connection.close() 는 finally 가 수행했다.
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
    _LOGGER.info("chunking worker 종료")


if __name__ == "__main__":
    main()
