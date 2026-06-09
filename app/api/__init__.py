"""app.api — Data Ingestion Pipeline FastAPI 앱 및 라우트.

수집 파이프라인을 BFF 에 노출하는 HTTP 계층. API 계약은 api-spec v2.2.0 §2-2/§2-3/§2-4-2.

모듈:
- main.py    FastAPI 앱 생성(create_app), 헬스 체크(/healthz), lifespan(build_ingest_deps).
             uvicorn 진입점.
- routes.py  POST /ml/ingest(트리거) / GET /ml/ingest/status/{jobId}(상태) /
             GET /ml/ingest/health(헬스).
- deps.py    IngestDeps 부트스트랩 — 잡 저장소(InMemoryIngestJobStore) + 크롤 러너
             (run_poc_ingestion in-process 합성).

이 계층은 요청 검증·응답 변환만 담당하고 수집 로직은 app.ingestion 에 둔다.
"""

from app.api.main import app, create_app

__all__ = [
    "app",
    "create_app",
]
