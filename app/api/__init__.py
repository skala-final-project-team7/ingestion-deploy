"""app.api — Data Ingestion Pipeline FastAPI 앱 및 라우트.

작성자 : 최태성
담당 영역 : ingestion

수집 파이프라인을 BFF 에 노출하는 HTTP 계층. API 계약은 api-spec v2.5.0
§2-2/§2-3/§2-4-2(+ 수집 완료 RabbitMQ completion event).

모듈:
- main.py              FastAPI 앱 생성(create_app), 헬스 체크(/healthz),
                       lifespan(build_ingest_deps). uvicorn 진입점.
- routes.py            POST /ml/ingest(트리거 — mode full/delta 분기) /
                       GET /ml/ingest/status/{jobId}(상태) / GET /ml/ingest/health(헬스).
- ingest_completion.py 수집 terminal(COMPLETED/FAILED) 상태의 RabbitMQ completion
                       event 발행(v2.5.0 — credential 미포함, Admin Key 말소 트리거).
- webhook_routes.py    Confluence 삭제 Webhook 수신(POST /ml/confluence/webhook) —
                       soft-delete 트리거(옵션 공유 시크릿 검증).
- deps.py              IngestDeps 부트스트랩 — 잡 저장소(InMemoryIngestJobStore) +
                       크롤/델타 러너 + completion publisher(기본 Noop).

이 계층은 요청 검증·응답 변환만 담당하고 수집 로직은 app.ingestion 에 둔다.
"""

from app.api.main import app, create_app

__all__ = [
    "app",
    "create_app",
]
