"""수집 HTTP API 의존성 부트스트랩 — 잡 저장소 + 크롤 러너 조립 [Pipeline].

--------------------------------------------------
작성자 : 최태성
작성목적 : ``POST /ml/ingest`` 라우트가 사용하는 의존성을 ``Settings`` 기반으로 조립한다.
          공급원 어댑터(``build_source_adapter`` — json_fixture | atlassian)를 startup 1회
          생성해 잡 간 재사용하고, 잡 수명주기 저장소(``InMemoryIngestJobStore``)와
          크롤 러너(in-process 합성 파이프라인)를 묶어 ``IngestDeps`` 로 제공한다.
작성일 : 2026-05-29 (api-spec v2.2.0 §2-2/§2-3 HTTP API)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-29, 최초 작성 — build_ingest_deps (job_store + run_crawl). run_crawl 은
    ``run_poc_ingestion`` 으로 crawl→chunk→upsert 를 in-process 합성 실행한다(PoC 전부 fake
    스토어 격리). 운영 분산 모드(RabbitMQ 워커 발행)는 후속 확장 지점.
  - 2026-06-09, api-spec v2.5.0 정합 — IngestDeps 에 ``completion_publisher`` 추가. 기본값은
    local/PoC 안전성을 위해 ``NoopIngestCompletionPublisher`` 이며, 운영 RabbitMQ 발행 wiring 은
    infra/worker 진입점에서 주입한다.
  - 2026-06-09, FR-005 delta 라우팅 — IngestDeps 에 ``run_delta``/``previous_snapshot_path`` 추가.
    기본값은 PoC 안전(변경분 없음)이며, 운영 delta(``run_delta_sync`` 실 client/snapshot)는
    infra/worker 진입점에서 주입한다.
  - 2026-06-09, FR-005 delta 삭제 적용 — IngestDeps 에 ``delta_delete_confirm`` 추가(기본 False=
    후보 surface만; True 시 delta 잡이 apply_delta_deletions(confirm=True)로 soft-delete).
--------------------------------------------------
[호환성]
  - Python 3.11.x
--------------------------------------------------
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.adapters.base import DocumentSourceAdapter
from app.adapters.factory import build_source_adapter
from app.api.ingest_completion import IngestCompletionPublisher, NoopIngestCompletionPublisher
from app.config import Settings
from app.ingestion.bootstrap import build_soft_delete_store
from app.ingestion.crawler import CrawlRequest, CrawlResult
from app.ingestion.pipeline import run_poc_ingestion
from app.ingestion.sync import DeltaSyncRequest, DeltaSyncResult
from app.ingestion.workers.sync_worker import SyncWorker, SyncWorkerDeps
from app.storage.ingest_jobs import IngestJobStore, InMemoryIngestJobStore

# 크롤 러너 시그니처 — ``CrawlRequest`` 를 받아 집계 ``CrawlResult`` 를 돌려준다.
CrawlRunner = Callable[[CrawlRequest], CrawlResult]
# Delta 러너 시그니처 — ``DeltaSyncRequest`` 를 받아 집계 ``DeltaSyncResult`` 를 돌려준다.
DeltaRunner = Callable[[DeltaSyncRequest], DeltaSyncResult]


def _poc_empty_delta(_request: DeltaSyncRequest) -> DeltaSyncResult:
    """PoC 기본 delta 러너 — 변경분 없음(외부 의존성 0).

    vendored Data Sync Agent(``run_delta_sync``)는 실 Confluence client + 직전 스냅샷을 요구하므로,
    PoC/local 기본 경로는 변경분 없음으로 안전 동작한다. 운영 delta 는 worker/infra 진입점에서 실
    client·snapshot 을 주입한 ``run_delta_sync`` 러너로 교체한다(completion_publisher 와 동일 패턴).
    """
    return DeltaSyncResult()


@dataclass
class IngestDeps:
    """수집 HTTP API 의존성 묶음 — 라우트와 백그라운드 잡 태스크가 공유한다."""

    job_store: IngestJobStore
    run_crawl: CrawlRunner
    sync_worker: SyncWorker
    # api-spec v2.5.0 — 수집 terminal 상태에서 발행할 completion event publisher.
    # 기본값 None(또는 Noop)이면 발행하지 않는다(local/PoC 안전). 운영 RabbitMQ wiring 은
    # infra/worker 진입점에서 주입한다.
    completion_publisher: IngestCompletionPublisher | None = None
    # FR-005 — mode=delta 러너 + 이전 스냅샷 경로(DeltaSyncRequest 구성용). 기본값은 PoC 안전(변경분
    # 없음)이며, 운영 delta 는 infra/worker 진입점에서 run_delta_sync 로 교체 주입한다.
    run_delta: DeltaRunner = _poc_empty_delta
    previous_snapshot_path: str = ""
    # FR-005 — delta 삭제 후보 자동 soft-delete 확인 게이트. 기본 False(후보 surface만);
    # True 시 delta 잡이 apply_delta_deletions(confirm=True)로 soft-delete 한다.
    delta_delete_confirm: bool = False


def build_ingest_deps(settings: Settings) -> IngestDeps:
    """``Settings`` 로 수집 API 의존성을 조립한다.

    - 공급원 어댑터: ``Settings.source_type`` 에 따라 json_fixture(샘플) 또는 atlassian(실
      Confluence)을 startup 에서 1회 생성해 잡 간 재사용한다.
    - 크롤 러너: ``run_poc_ingestion`` 으로 crawl→chunk→upsert 전 체인을 in-process 로
      합성 실행한다(``app/ingestion/pipeline.py`` 의 PoC 합성 — fake 스토어로 격리). 운영
      분산 모드(crawl/worker 를 RabbitMQ 로 분리)는 본 러너만 교체하면 되도록 격리한다.

    Args:
        settings: 환경 설정(``source_type`` / ``samples_dir`` / 자격증명 등).

    Returns:
        잡 저장소 + 크롤 러너를 묶은 ``IngestDeps``.
    """
    source: DocumentSourceAdapter = build_source_adapter(settings)

    def _run_crawl(request: CrawlRequest) -> CrawlResult:
        # in-process 합성 파이프라인(crawl→chunk→upsert). PoC 는 전부 fake 스토어로 격리
        # 실행하므로 외부 컨테이너·모델 없이 동작한다. 반환 ``CrawlResult`` 로 잡 카운트를 채운다.
        result, _components = run_poc_ingestion(request, source)
        return result.crawl

    # 삭제 트리거(Webhook 라우트)용 Sync Worker — soft-delete store 를 소유한다(featureI-5b).
    # HTTP 경로는 실시간 webhook 만 쓰므로 trash_source 는 주입하지 않는다(주기 Trash 동기화는
    # 스케줄러/실행 loop 책임 — featureI-7c). PoC store 는 ingest 합성 파이프라인과 분리된다.
    sync_worker = SyncWorker(SyncWorkerDeps(store=build_soft_delete_store(settings)))

    return IngestDeps(
        job_store=InMemoryIngestJobStore(),
        run_crawl=_run_crawl,
        sync_worker=sync_worker,
        # local/PoC 안전 기본값 — 운영 RabbitMQ 발행 wiring 은 worker/infra 진입점에서 주입한다.
        completion_publisher=NoopIngestCompletionPublisher(),
        # FR-005 — delta 러너는 PoC 안전 기본값(변경분 없음). 운영 delta 는 infra 진입점에서
        # ``run_delta_sync``(실 client/snapshot)로 교체한다. 스냅샷 경로만 설정에서 주입.
        previous_snapshot_path=settings.data_sync_previous_snapshot,
        delta_delete_confirm=settings.data_sync_delta_delete_confirm,
    )
