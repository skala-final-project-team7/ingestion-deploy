"""수집 잡 수명주기 저장소 — `/ml/ingest` 트리거·상태 조회 [Storage 경계].

--------------------------------------------------
작성자 : 최태성
담당 영역 : ingestion
작성목적 : api-spec v2.2.0 §2-2/§2-3 의 수집 HTTP API 가 사용하는 **잡 수명주기** 저장소를
          정의한다. ``POST /ml/ingest`` 가 잡을 생성(``STARTED``)하고 백그라운드 크롤이
          진행하며 상태(``IN_PROGRESS`` → ``COMPLETED``|``FAILED``)와 집계 카운트
          (total/processed/failed pages)를 갱신하면, ``GET /ml/ingest/status/{jobId}`` 가
          이를 조회한다. 페이지 단위 처리 로그(``app/storage/jobs.py`` ``IngestionJobRecord``,
          db-schema §2.3)와는 책임이 다르다 — 본 저장소는 잡 1건의 진행 상태만 추적한다.
작성일 : 2026-05-29 (api-spec v2.2.0 §2-2/§2-3 HTTP API)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-29, 최초 작성 — IngestJobRecord + IngestJobStore ABC + InMemoryIngestJobStore
    (PoC/단일 프로세스). 운영 다중 워커 환경은 공유 저장소(MySQL/Redis) 구현으로 교체한다.
  - 2026-06-10, 배포 전 점검 — (1) ``create(job_id=...)`` 로 외부(BFF) 생성 jobId 수용
    (api-spec v2.5.0 §2-2 — jobId 는 "BFF 가 생성하거나 Pipeline 이 생성"). (2) ``get``/
    ``update`` 가 내부 레코드의 **스냅샷**을 반환 — 백그라운드 태스크의 필드별 갱신과
    상태 조회 라우트가 같은 객체를 공유해 생기던 torn read(예: COMPLETED인데 카운트 0)
    제거.
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - 외부 의존성 0 (표준 라이브러리만 사용 — threading/uuid/datetime/dataclasses)
--------------------------------------------------
"""

from __future__ import annotations

import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from app.schemas.enums import IngestJobStatus


@dataclass
class IngestJobRecord:
    """수집 잡 1건의 수명주기 상태 (api-spec v2.2.0 §2-3 응답 필드의 내부 표현).

    저장 시각은 UTC(`datetime`, tz-aware)로 보관하고, API 직렬화 단계에서 KST(+09:00)로
    절대 전환한다(시간 표기 정책). ``total_pages`` / ``processed_pages`` / ``failed_pages``
    는 크롤 완료 시 ``CrawlResult`` 집계로 채운다.
    """

    job_id: str
    status: IngestJobStatus
    started_at: datetime
    total_pages: int = 0
    processed_pages: int = 0
    failed_pages: int = 0
    finished_at: datetime | None = None
    error: str | None = None


class IngestJobStore(ABC):
    """수집 잡 수명주기 저장소 인터페이스 — 라우트·백그라운드 태스크가 공유한다."""

    @abstractmethod
    def create(self, job_id: str | None = None) -> IngestJobRecord:
        """``STARTED`` 상태의 새 잡을 생성해 반환한다.

        Args:
            job_id: 외부(BFF)가 생성해 전달한 작업 식별자(api-spec v2.5.0 §2-2).
                None 이면 고유 ``job_id`` 를 새로 부여한다.
        """

    @abstractmethod
    def get(self, job_id: str) -> IngestJobRecord | None:
        """``job_id`` 로 잡을 조회한다. 없으면 None(라우트가 404로 매핑).

        구현은 호출자와 내부 상태가 객체를 공유하지 않도록 **스냅샷**을 반환해야 한다.
        """

    @abstractmethod
    def update(self, job_id: str, **changes: object) -> IngestJobRecord | None:
        """잡의 필드를 부분 갱신한다(존재하지 않으면 None). 반환값은 스냅샷."""


class InMemoryIngestJobStore(IngestJobStore):
    """프로세스 메모리 기반 잡 저장소 (PoC/단일 워커).

    백그라운드 크롤 태스크와 상태 조회 라우트가 서로 다른 스레드에서 접근하므로
    ``threading.Lock`` 으로 보호한다. 운영 다중 워커 환경에서는 공유 저장소 구현으로
    교체한다(본 클래스만 갈아끼우면 됨).
    """

    def __init__(self) -> None:
        self._jobs: dict[str, IngestJobRecord] = {}
        self._lock = threading.Lock()

    def create(self, job_id: str | None = None) -> IngestJobRecord:
        with self._lock:
            record = IngestJobRecord(
                job_id=job_id or f"job-{uuid.uuid4()}",
                status=IngestJobStatus.STARTED,
                started_at=datetime.now(UTC),
            )
            self._jobs[record.job_id] = record
            return replace(record)

    def get(self, job_id: str) -> IngestJobRecord | None:
        with self._lock:
            record = self._jobs.get(job_id)
            # 라이브 레코드를 그대로 반환하면 백그라운드 태스크의 필드별 setattr 와
            # 라우트의 직렬화가 같은 객체에서 교차해 torn read 가 된다 — 락 안에서
            # 스냅샷을 만들어 반환한다.
            return None if record is None else replace(record)

    def update(self, job_id: str, **changes: object) -> IngestJobRecord | None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            for key, value in changes.items():
                setattr(record, key, value)
            return replace(record)
