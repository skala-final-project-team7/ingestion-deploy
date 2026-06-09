"""FastAPI 앱 entrypoint — Data Ingestion Pipeline HTTP API [Pipeline].

--------------------------------------------------
작성자 : 최태성
작성목적 : 수집 파이프라인을 BFF 에 노출하는 HTTP 계층의 진입점. lifespan 에서 ``Settings``
          기반으로 ``build_ingest_deps`` 를 호출해 잡 저장소 + 크롤 러너를 부트스트랩하고
          ``app.state.ingest_deps`` 에 보관한다. 라우트는 ``app.api.routes`` 의 라우터를
          마운트한다. CORS·인증 미들웨어는 BFF 가 담당하므로 본 앱은 추가하지 않는다
          (api-spec NOTE — RAG Pipeline 앱과 동일 방침).
작성일 : 2026-05-29 (api-spec v2.2.0 §2-2/§2-3/§2-4-2)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-29, 최초 작성 — create_app + lifespan(build_ingest_deps) + 헬스 라우트(/healthz).
  - 2026-06-04, /metrics 노출 — Prometheus instrumentator wiring 추가(RAG Pipeline 앱과
    동일 패턴). HTTP 표준 메트릭(요청 수·지연 히스토그램·상태 코드별 카운터)을 ``/metrics`` 로
    노출한다. BFF 인증을 우회하는 Prometheus scraper 직접 접근 경로이며 OpenAPI 스키마에서는
    제외(include_in_schema=False)한다. 워커 커스텀 메트릭(prometheus-client)은 워커 프로세스가
    별도로 노출한다.
--------------------------------------------------
[호환성]
  - Python 3.11.x, FastAPI 0.111+
  - 실행 예시: ``uvicorn app.api.main:app --host 0.0.0.0 --port 8001``
--------------------------------------------------
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.deps import build_ingest_deps
from app.api.routes import router as ingest_router
from app.api.webhook_routes import webhook_router
from app.config import get_settings


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """앱 시작 시 수집 의존성(잡 저장소 + 크롤 러너)을 한 번 조립해 app.state 에 보관한다."""
    settings = get_settings()
    app.state.settings = settings
    app.state.ingest_deps = build_ingest_deps(settings)
    try:
        yield
    finally:
        app.state.ingest_deps = None
        app.state.settings = None


def create_app() -> FastAPI:
    """FastAPI 앱 인스턴스를 생성한다 — 운영·테스트 공통 팩토리.

    테스트는 ``create_app()`` 후 ``app.dependency_overrides[get_deps]`` 로 의존성을 교체하거나
    ``app.state.ingest_deps`` 를 수동 설정한다.
    """
    app = FastAPI(
        title="LINA Data Ingestion Pipeline",
        version="0.1.0",
        description="척척학사(LINA) Confluence 기반 RAG 챗봇 서비스의 데이터 수집 파이프라인",
        lifespan=_lifespan,
    )
    app.include_router(ingest_router)
    app.include_router(webhook_router)

    # 운영 모니터링 — Prometheus instrumentator. ``/metrics`` 로 HTTP 표준 메트릭
    # (요청 수·지연 히스토그램·상태 코드별 카운터)을 노출한다. RAG Pipeline 앱과 동일
    # 패턴 — BFF 인증을 우회하는 Prometheus scraper 직접 접근 경로이며 OpenAPI 스키마에서는
    # 제외(include_in_schema=False)한다. 워커 잡 카운터·지연(prometheus-client)은 워커
    # 프로세스가 별도로 노출한다.
    Instrumentator().instrument(app).expose(
        app,
        endpoint="/metrics",
        include_in_schema=False,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """기본 헬스 체크 — Kubernetes readiness probe 대상."""
        return {"status": "ok"}

    return app


# uvicorn 진입점 (``uvicorn app.api.main:app``).
app = create_app()
