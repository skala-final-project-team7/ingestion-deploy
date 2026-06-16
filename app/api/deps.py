"""수집 HTTP API 의존성 부트스트랩 — 잡 저장소 + 크롤 러너 조립 [Pipeline].

--------------------------------------------------
작성자 : 최태성
담당 영역 : ingestion
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
  - 2026-06-10, 코드 리뷰 재점검(A16) — 운영 wiring 안내를 bootstrap 헬퍼
    (build_ingest_completion_publisher / build_delta_runner) 기준으로 갱신(한 줄 주입).
  - 2026-06-11, 배포 전 점검 fix — ``use_real_adapters=True`` 운영 분기 추가
    (_build_real_ingest_deps). 종전에는 토글과 무관하게 run_crawl=run_poc_ingestion(전부
    Fake store) / run_delta=변경 0건 / completion=Noop 으로 고정되어, 운영 배포에서도 실
    Confluence 크롤 결과가 인메모리 fake 에 적재된 뒤 소멸했다(실 Qdrant 미적재 무음 결함).
    운영 분기는 bootstrap 의 build_real_crawl_runner / build_real_delta_runner /
    build_real_completion_publisher 를 소비한다(RabbitMQ 연결은 잡 단위 — threadpool 안전).
--------------------------------------------------
[호환성]
  - Python 3.11.x
--------------------------------------------------
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.adapters.base import DocumentSourceAdapter
from app.adapters.factory import build_source_adapter
from app.api.ingest_completion import IngestCompletionPublisher, NoopIngestCompletionPublisher
from app.config import Settings
from app.ingestion.bootstrap import build_internal_credential_lookup, build_soft_delete_store
from app.ingestion.crawler import CrawlRequest, CrawlResult
from app.ingestion.pipeline import run_poc_ingestion
from app.ingestion.sync import DeltaSyncRequest, DeltaSyncResult
from app.ingestion.workers.sync_worker import SyncWorker, SyncWorkerDeps
from app.storage.ingest_jobs import IngestJobStore, InMemoryIngestJobStore

_LOGGER = logging.getLogger(__name__)

# 크롤 러너 시그니처 — ``CrawlRequest`` 를 받아 집계 ``CrawlResult`` 를 돌려준다.
CrawlRunner = Callable[[CrawlRequest], CrawlResult]
# Delta 러너 시그니처 — ``DeltaSyncRequest`` 를 받아 집계 ``DeltaSyncResult`` 를 돌려준다.
DeltaRunner = Callable[[DeltaSyncRequest], DeltaSyncResult]
CredentialLookup = Callable[[str], tuple[str | None, str | None]]


def _build_internal_credential_lookup(settings: Settings) -> CredentialLookup | None:
    """auth-server credential_lookup seam 을 준비한다.

    운영에서는 adminUserId 기반 credential 조회에 사용한다.
    bootstrap 설정 미완성 시에는 시작 실패를 피하고 `None` 으로 되돌린다(기존 PoC 경로 유지를 위해).
    """
    if settings.source_type.lower() != "atlassian":
        return None
    if not settings.internal_auth_server_base_url.strip():
        _LOGGER.warning(
            "RAG_INTERNAL_AUTH_SERVER_BASE_URL 비어 있어 auth-server credential lookup 비활성화"
        )
        return None
    if not settings.internal_api_key.get_secret_value():
        _LOGGER.warning(
            "RAG_INTERNAL_API_KEY 비어 있어 auth-server credential lookup 비활성화"
        )
        return None

    try:
        return build_internal_credential_lookup(settings)
    except ValueError:
        _LOGGER.exception("auth-server credential lookup 클라이언트 구성 실패")
        return None


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
    # auth-server 내부 API(``adminUserId`` 기준) credential 조회 seam.
    # 기본 None 이면 요청으로 전달된 access_token/cloud_id fallback 사용.
    credential_lookup: CredentialLookup | None = None


def build_ingest_deps(settings: Settings) -> IngestDeps:
    """``Settings`` 로 수집 API 의존성을 조립한다 — ``use_real_adapters`` 로 PoC/운영 분기.

    - **PoC** (``use_real_adapters=False``, 기본): ``run_poc_ingestion`` 으로 crawl→chunk→
      upsert 전 체인을 in-process 합성 실행한다(전부 fake 스토어 격리 — 외부 컨테이너 0).
      delta 는 변경 0건, completion event 는 Noop.
    - **운영** (``use_real_adapters=True``): full crawl 은 실 raw_store(Mongo) + Mongo jobs
      에 적재하고 ``content.chunking`` 을 실 RabbitMQ 로 발행한다(색인은 chunking worker —
      ``python -m app.ingestion.workers.chunking_main`` 가 소비). delta 는 ``run_delta_sync``
      실 러너, completion event 는 실 RabbitMQ publisher. (배포 전 점검 fix, 2026-06-11 —
      종전에는 운영 토글과 무관하게 PoC 합성으로 고정되어, 실 Confluence 를 크롤링하고도
      결과가 인메모리 fake 에 적재된 뒤 소멸했다.)

    Args:
        settings: 환경 설정(``source_type`` / ``samples_dir`` / 자격증명 등).

    Returns:
        잡 저장소 + 크롤/델타 러너 + completion publisher 를 묶은 ``IngestDeps``.
    """
    # 삭제 경로(Webhook 라우트 + delta 삭제 후보 적용)용 Sync Worker — soft-delete store 를
    # 소유한다(featureI-5b·FR-005). trash_source 는 주입하지 않는다 — 주기 Trash 동기화는
    # 스케줄러/실행 loop 책임(featureI-7c). store 는 토글에 따라 Fake/실 Qdrant 로 조립된다.
    sync_worker = SyncWorker(SyncWorkerDeps(store=build_soft_delete_store(settings)))
    credential_lookup = _build_internal_credential_lookup(settings)

    if settings.use_real_adapters:
        return _build_real_ingest_deps(settings, sync_worker=sync_worker)

    source: DocumentSourceAdapter = build_source_adapter(settings)

    def _run_crawl(request: CrawlRequest) -> CrawlResult:
        # in-process 합성 파이프라인(crawl→chunk→upsert). PoC 는 전부 fake 스토어로 격리
        # 실행하므로 외부 컨테이너·모델 없이 동작한다. 반환 ``CrawlResult`` 로 잡 카운트를 채운다.
        result, _components = run_poc_ingestion(request, source)
        return result.crawl

    return IngestDeps(
        job_store=InMemoryIngestJobStore(),
        run_crawl=_run_crawl,
        sync_worker=sync_worker,
        # PoC 안전 기본값 — completion 발행 없음 / delta 변경 0건(IngestDeps 기본 러너).
        completion_publisher=NoopIngestCompletionPublisher(),
        previous_snapshot_path=settings.data_sync_previous_snapshot,
        delta_delete_confirm=settings.data_sync_delta_delete_confirm,
        credential_lookup=credential_lookup,
    )


def _build_real_ingest_deps(settings: Settings, *, sync_worker: SyncWorker) -> IngestDeps:
    """운영 IngestDeps 조립 — 실 Mongo/RabbitMQ wiring (bootstrap 헬퍼 소비).

    어댑터 정책: api-spec v2.5.0 흐름은 BFF 가 잡마다 access_token/cloud_id 를 전달하므로
    Settings 자격증명이 비어 있어도 부팅은 성공해야 한다. 따라서 startup 어댑터는
    **best-effort** 로만 만들고(자격증명 미비 시 None — 요청 자격증명 경로만 사용), 잡
    단위 어댑터 결정은 ``build_real_crawl_runner`` 가 한다. RabbitMQ 연결은 잡/발행
    단위로 열고 닫아 BackgroundTasks threadpool 동시 실행에 안전하다.
    """
    from app.adapters.factory import MissingAtlassianCredentialsError
    from app.ingestion.bootstrap import (
        build_raw_page_store,
        build_real_completion_publisher,
        build_real_crawl_runner,
        build_real_delta_runner,
    )

    try:
        fallback_source: DocumentSourceAdapter | None = build_source_adapter(settings)
    except MissingAtlassianCredentialsError:
        # v2.5.0 — 자격증명은 요청으로 들어온다. startup fallback 어댑터 없이 진행한다
        # (요청에도 자격증명이 없으면 crawl 러너가 RuntimeError 로 잡을 FAILED 처리).
        fallback_source = None

    raw_store = build_raw_page_store(settings)  # Mongo raw_pages/raw_attachments (crawl·delta 공유)
    credential_lookup = _build_internal_credential_lookup(settings)

    return IngestDeps(
        # NOTE(P2): 잡 수명주기 저장소는 단일 인스턴스 전제의 InMemory 다(재시작 시 진행
        # 중 잡 상태 유실 — api-spec idempotent jobId 재요청으로 완화). durable 승급은 후속.
        job_store=InMemoryIngestJobStore(),
        run_crawl=build_real_crawl_runner(
            settings, fallback_source=fallback_source, raw_store=raw_store
        ),
        sync_worker=sync_worker,
        completion_publisher=build_real_completion_publisher(settings),
        run_delta=build_real_delta_runner(settings, raw_store=raw_store),
        previous_snapshot_path=settings.data_sync_previous_snapshot,
        delta_delete_confirm=settings.data_sync_delta_delete_confirm,
        credential_lookup=credential_lookup,
    )
