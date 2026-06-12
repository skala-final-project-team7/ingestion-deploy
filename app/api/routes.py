"""수집 HTTP API 라우트 — POST /ml/ingest + status + health [Pipeline].

--------------------------------------------------
작성자 : 최태성
담당 영역 : ingestion
작성목적 : api-spec v2.4.0 §2-2/§2-3/§2-4-2 의 수집(Data Ingestion) HTTP 계약을 제공한다.
          ``POST /ml/ingest`` 는 잡을 생성(``STARTED``)하고 백그라운드에서 crawl→chunk→
          upsert 를 실행하며, ``GET /ml/ingest/status/{jobId}`` 가 진행 상태·집계 카운트를,
          ``GET /ml/ingest/health`` 가 서버 가용성을 반환한다. 응답은 BFF 가 공통 Wrapper 로
          감싸므로 ML 은 **data 객체를 그대로(unwrapped)** 반환한다(§2-3 "외부 API data 동일",
          §2-4 health 선례와 정합).
작성일 : 2026-05-29 (api-spec v2.2.0 §2-2/§2-3/§2-4-2)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-29, 최초 작성 — IngestRequest(spaceKey/mode/accessToken/cloudId) + POST 트리거
    (BackgroundTasks 로 비동기 크롤) + status 조회(KST startedAt) + health.
  - 2026-06-04, api-spec v2.4.0 정합 — IngestRequest 에서 ``spaceKey`` 필드 **제거**(스페이스
    스코프 파라미터 없음 — admin Key 로 접근 가능한 전체 스페이스 iterate, 2026-06-04 결정).
    요청 본문은 ``mode``/``accessToken``/``cloudId`` 만. CrawlRequest 는 space_key 미지정(전체).
  - 2026-06-09, api-spec v2.5.0 정합 — Admin Key 말소 트리거를 BFF HTTP callback 에서 RabbitMQ
    completion event 로 전환. ``adminUserId`` 를 preferred job 식별자로 추가하고, terminal
    (COMPLETED/FAILED) 상태에서 credential 없는 completion event 를 발행한다. ``accessToken``/
    ``cloudId`` 직접 전달은 legacy PoC 호환 필드로만 유지한다.
  - 2026-06-09, FR-005 delta 라우팅 — ``mode=delta`` 를 full-crawl 이 아니라 Delta Sync
    (``deps.run_delta``)로 분기하고, terminal 상태에서 completion event(mode="delta")를 발행한다.
--------------------------------------------------
[보안] 요청 ``accessToken``/``cloudId`` 는 로그·응답 본문·completion event payload 에 남기지
       않는다(루트 CLAUDE.md 보안 규칙). 상태 응답·completion event 에는 토큰 관련 필드를 포함하지
       않으며, completion event 식별자로는 credential 이 아닌 ``adminUserId`` 만 싣는다.
[호환성]
  - Python 3.11.x, FastAPI 0.111+
--------------------------------------------------
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import IngestDeps
from app.api.ingest_completion import IngestCompletionEvent, publish_ingest_completion_safely
from app.ingestion.crawler import CrawlRequest
from app.ingestion.sync import DeltaSyncRequest
from app.schemas.enums import IngestJobStatus

_LOGGER = logging.getLogger(__name__)

# api-spec "시간 표기 정책" — 응답 timestamp 는 KST(+09:00)로 절대 전환해 반환한다.
_KST = timezone(timedelta(hours=9))

# 허용 수집 모드(api-spec §2-2). full 은 전체 크롤, delta 는 Delta Sync(FR-005, 2026-06-09
# 배선 — ``deps.run_delta``)로 분기한다(``ingest_route`` 의 mode 분기 참조).
_ALLOWED_MODES: frozenset[str] = frozenset({"full", "delta"})

router = APIRouter()


def _to_kst(dt: datetime) -> str:
    """UTC(또는 naive) datetime 을 KST(+09:00) ISO 8601 문자열로 변환한다."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_KST).isoformat()


class IngestRequest(BaseModel):
    """``POST /ml/ingest`` 요청 본문 (api-spec v2.5.0 §2-2).

    Preferred 운영 경로는 RabbitMQ ingest job 또는 HTTP 위임 payload 에 credential set 을 싣지
    않고 ``adminUserId`` 만 전달한다. Data Ingestion Worker 는 auth-server 내부 credential API 로
    admin OAuth ``accessToken`` + ``cloudId`` 를 조회한다. ``accessToken``/``cloudId`` 는 backend
    OAuth 완성 전 local/PoC smoke 호환 필드로만 남기며, production RabbitMQ job/completion payload
    에는 절대 포함하지 않는다.

    api-spec v2.4.0 §2-2 — **스페이스 스코프 파라미터(``spaceKey``) 없음**. admin Key 로 admin 이
    접근 가능한 **전체 스페이스**를 ML 이 iterate 하며 수집한다. ``populate_by_name=True`` 로
    snake_case 입력도 허용한다(테스트 편의).
    """

    model_config = ConfigDict(populate_by_name=True)

    mode: str = Field(default="full", description="수집 모드 — full(전체) | delta(변경분)")
    # api-spec v2.5.0 §2-2 — jobId 는 "BFF 가 생성하거나 Data Ingestion Pipeline 이 생성해
    # 반환"한다. BFF 가 보낸 값은 그대로 잡 식별자로 사용해 completion event·status 조회·
    # Admin Key deactivate idempotency 의 기준을 일치시키고, 없으면 서버가 발급한다.
    job_id: str | None = Field(
        default=None,
        alias="jobId",
        description="작업 식별자(BFF 생성 시 전달). 없으면 Pipeline 이 발급해 반환한다.",
    )
    admin_user_id: str | None = Field(
        default=None,
        alias="adminUserId",
        description="Admin Confluence accountId. api-spec v2.5.0 preferred credential lookup key.",
    )
    access_token: str | None = Field(
        default=None,
        alias="accessToken",
        description="Legacy PoC-only admin Confluence OAuth access token. 로그/큐/응답 금지.",
    )
    cloud_id: str | None = Field(
        default=None,
        alias="cloudId",
        description="Legacy PoC-only Confluence cloudId. RabbitMQ payload 포함 금지.",
    )

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        """mode 는 ``full`` | ``delta`` 만 허용한다(api-spec §2-2)."""
        normalized = value.strip().lower()
        if normalized not in _ALLOWED_MODES:
            raise ValueError(f"mode 는 full | delta 여야 합니다 (받음: {value!r})")
        return normalized


def get_deps(request: Request) -> IngestDeps:
    """FastAPI Depends — lifespan 에서 만든 수집 의존성을 반환한다.

    테스트는 ``app.dependency_overrides[get_deps] = lambda: fake_deps`` 로 교체할 수 있다.
    """
    return request.app.state.ingest_deps


IngestDepsDep = Annotated[IngestDeps, Depends(get_deps)]


def _run_ingest_job(deps: IngestDeps, job_id: str, mode: str, crawl_request: CrawlRequest) -> None:
    """백그라운드 수집 잡 — 상태를 ``IN_PROGRESS`` 로 올리고 크롤 실행 후 마감한다.

    크롤 성공 시 ``CrawlResult`` 집계로 카운트를 채워 ``COMPLETED`` 로, 예외 시 ``FAILED``
    로 마감한다(예외는 잡 단위로 격리 — 서버 전체로 전파하지 않는다). 토큰은 로그에 남기지
    않는다(``crawl_request`` 전체를 로깅하지 않고 ``job_id`` 만 기록). terminal 상태 도달 후에는
    api-spec v2.5.0 completion event 를 발행한다(발행 실패는 잡 상태를 덮어쓰지 않는다).
    """
    deps.job_store.update(job_id, status=IngestJobStatus.IN_PROGRESS)
    try:
        result = deps.run_crawl(crawl_request)
    except Exception as exc:  # noqa: BLE001 — 크롤/외부 호출 예외 광범위 캐치(잡 단위 격리)
        finished_at = datetime.now(UTC)
        _LOGGER.exception("ingest job failed: job_id=%s", job_id)
        deps.job_store.update(
            job_id,
            status=IngestJobStatus.FAILED,
            finished_at=finished_at,
            error=str(exc),
        )
        _publish_ingest_completion(
            deps,
            job_id=job_id,
            mode=mode,
            status=IngestJobStatus.FAILED,
            admin_user_id=crawl_request.admin_user_id,
            error_code="INGEST_FAILED",
            error=str(exc),
            finished_at=finished_at,
        )
        return
    finished_at = datetime.now(UTC)
    failed = len(result.failed_page_ids)
    deps.job_store.update(
        job_id,
        status=IngestJobStatus.COMPLETED,
        total_pages=result.pages_collected + failed,
        processed_pages=result.pages_collected,
        failed_pages=failed,
        finished_at=finished_at,
    )
    _publish_ingest_completion(
        deps,
        job_id=job_id,
        mode=mode,
        status=IngestJobStatus.COMPLETED,
        admin_user_id=crawl_request.admin_user_id,
        finished_at=finished_at,
    )


def _run_delta_ingest_job(deps: IngestDeps, job_id: str, delta_request: DeltaSyncRequest) -> None:
    """백그라운드 delta 수집 잡 — Delta Sync 실행 후 마감한다 (FR-005).

    ``deps.run_delta`` 로 변경분을 수집(vendored Data Sync Agent 래퍼)하고, ``DeltaSyncResult``
    집계로 카운트를 채워 ``COMPLETED`` 로, 예외 시 ``FAILED`` 로 마감한다(예외는 잡 단위 격리).
    상태 카운트는 processed=changed_pages / failed=failed_items / total=합 으로 매핑한다.
    삭제 후보(deleted_candidate_page_ids)는 확인 게이트(``deps.delta_delete_confirm``)가 켜진
    경우만 ``SyncWorker.apply_delta_deletions`` 로 soft-delete 한다(기본 OFF=surface만). terminal
    에서 completion event(mode="delta")를 발행한다.
    """
    deps.job_store.update(job_id, status=IngestJobStatus.IN_PROGRESS)
    try:
        result = deps.run_delta(delta_request)
    except Exception as exc:  # noqa: BLE001 — delta/외부 호출 예외 광범위 캐치(잡 단위 격리)
        finished_at = datetime.now(UTC)
        _LOGGER.exception("delta ingest job failed: job_id=%s", job_id)
        deps.job_store.update(
            job_id,
            status=IngestJobStatus.FAILED,
            finished_at=finished_at,
            error=str(exc),
        )
        _publish_ingest_completion(
            deps,
            job_id=job_id,
            mode="delta",
            status=IngestJobStatus.FAILED,
            admin_user_id=delta_request.admin_user_id,
            error_code="INGEST_FAILED",
            error=str(exc),
            finished_at=finished_at,
        )
        return
    finished_at = datetime.now(UTC)
    # 삭제 후보 soft-delete 적용(확인 게이트). 기본 confirm=False 면 no-op(후보 surface만).
    # apply_delta_deletions/apply_soft_deletes 는 id 단위로 실패를 격리하므로 예외를 전파하지
    # 않는다 — soft-delete 실패가 수집 잡을 FAILED 로 만들지 않는다(best-effort side-effect).
    delete_result = deps.sync_worker.apply_delta_deletions(
        result, confirm=deps.delta_delete_confirm
    )
    if delete_result.has_failures:
        _LOGGER.warning(
            "delta soft-delete partial failure: job_id=%s deleted=%d failed_pages=%d",
            job_id,
            delete_result.total_soft_deleted,
            len(delete_result.failed_page_ids),
        )
    elif delete_result.total_soft_deleted:
        _LOGGER.info(
            "delta soft-delete applied: job_id=%s deleted=%d",
            job_id,
            delete_result.total_soft_deleted,
        )
    deps.job_store.update(
        job_id,
        status=IngestJobStatus.COMPLETED,
        total_pages=result.changed_pages + result.failed_items,
        processed_pages=result.changed_pages,
        failed_pages=result.failed_items,
        finished_at=finished_at,
    )
    _publish_ingest_completion(
        deps,
        job_id=job_id,
        mode="delta",
        status=IngestJobStatus.COMPLETED,
        admin_user_id=delta_request.admin_user_id,
        finished_at=finished_at,
    )


def _publish_ingest_completion(
    deps: IngestDeps,
    *,
    job_id: str,
    mode: str,
    status: IngestJobStatus,
    admin_user_id: str | None,
    finished_at: datetime,
    error_code: str | None = None,
    error: str | None = None,
) -> None:
    """수집 terminal 상태 도달 후 RabbitMQ completion event 를 발행한다 (api-spec v2.5.0).

    ML 은 Atlassian Admin Key 를 직접 말소하지 않고 BFF HTTP callback 도 호출하지 않는다.
    BFF consumer 가 completion event 를 consume 하고 auth-server deactivate 내부 API 를 호출한다.
    event payload 에 credential set(accessToken/refreshToken/cloudId)은 포함하지 않으며, 발행
    실패는 이미 확정된 잡 terminal 상태를 되돌리거나 덮어쓰지 않는다(``publish_..._safely`` 격리).
    """
    publish_ingest_completion_safely(
        deps.completion_publisher,
        IngestCompletionEvent(
            job_id=job_id,
            mode=mode,
            status=status,
            admin_user_id=admin_user_id,
            error_code=error_code,
            message=error,
            completed_at=finished_at,
        ),
    )


@router.post("/ml/ingest")
async def ingest_route(
    payload: IngestRequest,
    background_tasks: BackgroundTasks,
    deps: IngestDepsDep,
) -> dict[str, Any]:
    """수집 트리거 (api-spec v2.5.0 §2-2).

    잡을 ``STARTED`` 로 생성하고 백그라운드 태스크로 crawl→chunk→upsert 를 실행한 뒤,
    즉시 ``jobId`` / ``status`` / ``startedAt``(KST)을 반환한다. 진행 상태는
    ``GET /ml/ingest/status/{jobId}`` 로 조회한다. 스페이스 스코프 파라미터는 없으며(v2.4.0),
    ``mode=full``(기본)은 admin Key 로 접근 가능한 전체 스페이스를 수집하고, ``mode=delta``는 직전
    스냅샷 대비 변경분만 Delta Sync 한다(FR-005). terminal(COMPLETED/FAILED) 상태 도달 시
    credential 없는 completion event 를 발행한다(v2.5.0 — Admin Key 말소 트리거).

    ``jobId``(§2-2)가 본문에 오면 그 값을 잡 식별자로 사용한다. 같은 ``jobId`` 재요청은
    잡을 새로 만들지 않고 기존 잡의 현재 상태를 반환한다(idempotent — completion event
    중복 처리 정책과 정합. 단일 인스턴스 전제는 InMemoryIngestJobStore 와 동일).
    """
    if payload.job_id:
        existing = deps.job_store.get(payload.job_id)
        if existing is not None:
            return {
                "jobId": existing.job_id,
                "status": existing.status.value,
                "startedAt": _to_kst(existing.started_at),
            }
    job = deps.job_store.create(job_id=payload.job_id)
    # mode 분기: ``delta`` 는 Delta Sync(vendored Data Sync Agent 래퍼)로, 그 외(``full``)는
    # full-crawl 합성으로 처리한다. credential 은 요청 객체로만 전달하고 로그·응답에 남기지 않는다.
    # ``adminUserId``(credential 아님)는 terminal completion event 식별자로 전달한다.
    if payload.mode == "delta":
        delta_request = DeltaSyncRequest(
            previous_snapshot_path=deps.previous_snapshot_path,
            access_token=payload.access_token,
            cloud_id=payload.cloud_id,
            admin_user_id=payload.admin_user_id,
        )
        background_tasks.add_task(_run_delta_ingest_job, deps, job.job_id, delta_request)
    else:
        # space_key 미지정(기본 "") → 전체 스페이스 수집(api-spec v2.4.0 §2-2).
        crawl_request = CrawlRequest(
            access_token=payload.access_token,
            cloud_id=payload.cloud_id,
            admin_user_id=payload.admin_user_id,
        )
        background_tasks.add_task(_run_ingest_job, deps, job.job_id, payload.mode, crawl_request)
    return {
        "jobId": job.job_id,
        "status": job.status.value,
        "startedAt": _to_kst(job.started_at),
    }


@router.get("/ml/ingest/status/{job_id}")
async def ingest_status_route(job_id: str, deps: IngestDepsDep) -> Any:
    """수집 상태 조회 (api-spec v2.2.0 §2-3).

    잡을 찾으면 ``jobId`` / ``status`` / ``totalPages`` / ``processedPages`` /
    ``failedPages`` / ``startedAt``(KST)를 반환한다. 없으면 4필드 에러 봉투로 404 응답.
    """
    record = deps.job_store.get(job_id)
    if record is None:
        return JSONResponse(
            status_code=404,
            content={
                "isSuccess": False,
                "code": 404,
                "errorCode": "RESOURCE_NOT_FOUND",
                "message": f"수집 작업을 찾을 수 없습니다: {job_id}",
            },
        )
    return {
        "jobId": record.job_id,
        "status": record.status.value,
        "totalPages": record.total_pages,
        "processedPages": record.processed_pages,
        "failedPages": record.failed_pages,
        "startedAt": _to_kst(record.started_at),
    }


@router.get("/ml/ingest/health")
async def ingest_health() -> dict[str, str]:
    """Data Ingestion Pipeline 헬스체크 (api-spec v2.2.0 §2-4-2).

    BFF 가 수집 서버(Confluence 수집/청킹/임베딩)가 정상 응답 가능한지만 확인하는 용도.
    내부 의존성(Vector DB / Confluence / RabbitMQ 등) 상세 상태는 보고하지 않고, 서버가
    요청을 받아 응답할 수 있는 상태인지만 ``{"status": "UP"}`` 로 알린다(§2-4 공통 규칙).
    """
    return {"status": "UP"}
