# INTEGRATION — ingestion-deploy (인프라 통합 가이드)

이 레포는 `ingestion`에서 **vendored 에이전트 2종을 발라낸** 배포 전용 버전이다. 에이전트는 별도 `ai-agent` 레포에서 설치된다. (원본: `skala-final-project-team7/ingestion`)

## 1. 무엇이 바뀌었나
- **제거**(이 레포에 없음): `data_ingestion_agent/`, `data_sync_agent/` (+ `tests/data_ingestion_agent/`, `tests/data_sync_agent/`)
- **pyproject**: vendoring 노출/제외 설정(`packages.find`의 에이전트, ruff/mypy 제외·override) 제거 → `[project].dependencies`에 ai-agent 의존성 1줄 추가. 선택 extra `agents`(langgraph)는 유지
- **app/ 코드·import·테스트: 무변경** — `from data_ingestion_agent ...`처럼 top-level import라 공급원(in-repo 사본 → 설치된 ai-agent)만 바뀐다
- **제외**: `Dockerfile`·`.dockerignore`·`.env.example`은 의도적으로 미포함(인프라가 별도 관리)

## 2. 에이전트 의존성
```toml
# pyproject.toml [project].dependencies
"lina-ai-agents @ git+https://github.com/skala-final-project-team7/ai-agent.git@v0.1.1",
```
app은 2개 에이전트를 **top-level 패키지명**으로 import한다(소스 6곳): `data_ingestion_agent`, `data_sync_agent`. ai-agent가 이 이름을 그대로 노출하면 import는 **무변경**으로 해결된다.

## 3. ✅ ai-agent 레포 선행 수정 — 해소됨 (2026-06-10)
`skala-final-project-team7/ai-agent` 커밋 `54055df` · 태그 **`v0.1.0`** 에서 아래 3건이 모두 반영됐다
(태그 시점 pyproject 직접 확인 — name/`packages.find` 검증 완료):

| # | 문제(당시 main `f9f458c`) | 적용된 수정 (v0.1.0) |
|---|---|---|
| 1 | `app` 패키지 충돌 — `packages.find.include`에 `"app*"` 포함 | `app*` 제거, 6개 에이전트 패키지만 노출 ✅ |
| 2 | 핀 가변 — release tag 없음(`@main`) | 태그 `v0.1.0` 발행 ✅ (현재 핀은 `@v0.1.1` — §3b) |
| 3 | (rag 공통) 배포 이름 `lina-rag-pipeline` | `lina-ai-agents` 로 변경 ✅ |

> top-level 에이전트 패키지명 6종(data_ingestion_agent/data_sync_agent 포함)은 무변경 — app 코드 import 그대로 동작.

## 3b. ✅ ai-agent v0.1.1 핀 확정 + admin-key config 정합 fix (2026-06-11)
ai-agent 태그 **`v0.1.1`**(= main `fbe522d`, annotated) 확정에 따라 핀을 `@v0.1.0` → **`@v0.1.1`** 로 교체했다.
v0.1.1 은 `data_ingestion_agent`/`data_sync_agent` 의 admin-key 모델을 확장한다(api-spec v2.6.1/2 정합):
`DataIngestionConfig`/`DataSyncConfig` 에 `site_url`/`admin_email`/`admin_api_token` 필드 신설 +
**`use_admin_key=True` 면 3필드 필수 검증(ValueError)** + vendored client 가 Basic+site URL 경로를 네이티브 내장.

본 레포는 종전(v0.1.0 기준) `use_admin_key=True` 를 3필드 없이 생성했으므로 **핀 교체만 하면 admin-key
운영 경로가 기동 시 즉사**한다(v0.1.1 런타임 실측: `ValueError: site_url is required when use_admin_key is true`).
동반 코드 수정(완료):

| 위치 | 변경 |
|---|---|
| `app/adapters/atlassian.py` `_build_admin_confluence_client` | config 에 `site_url`/`admin_email`/`admin_api_token` 실값 전달(기존 RuntimeError 가드로 비어 있지 않음 보장). `_AdminKeyBasicAuthConfluenceClient` 서브클래스는 v0.1.1 네이티브와 로직 동일(diff 확인) — 동작 고정 가드로 유지 |
| `app/adapters/atlassian.py` `AtlassianSourceAdapter` | `admin_email`/`admin_api_token` 파라미터 신설(기본 ""), `_build_config` 가 **admin-key 경로 한정**으로 3필드 전달(OAuth 경로 생성 형태 무변경). `from_settings` 가 Settings 에서 주입 |
| `app/ingestion/bootstrap.py` `build_request_source_adapter` | 어댑터 생성에 `admin_email`/`admin_api_token` 전달 추가 |

- delta sync (`DataSyncConfig` 4필드 생성, `use_admin_key` 기본 False)·OAuth 경로는 v0.1.1 에서도 무변경 동작(실측 OK).
- env 계약 무변경 — `RAG_ATLASSIAN_SITE_URL`/`RAG_ATLASSIAN_ADMIN_EMAIL`/`RAG_ATLASSIAN_ADMIN_API_TOKEN` 그대로.
- ⚠ 설치본이 stale v0.1.0 인 환경에서 admin-key 경로는 `TypeError: unexpected keyword argument 'site_url'` 로
  즉시 드러난다 — `pip install -e ...` 재실행으로 해소(의도된 loud-failure, 무음 드리프트 방지).

## 4. 빌드/검증 (Python 3.11)
```bash
pip install -e '.[embedding,ingestion,dev]'   # ai-agent v0.1.1 + 파서 + ruff/pytest
python -c "import app.api.main"            # 에이전트 import 해결 = ai-agent 설치 확인
./scripts/verify.sh                        # format → lint → test
```

> `verify.sh` 는 ruff·pytest 를 요구하므로 `dev` extra 가 필요하다.

## 4b. 운영 wiring (배포 전 점검 fix, 2026-06-11) — 인프라 커스텀 코드 불필요
종전 "비동기 워커 실행 loop = 인프라 소유(featureI-7c)" 항목은 본 레포가 흡수했다. 인프라는 **프로세스 2개를 띄우고 env 만 주입**하면 된다:

| 프로세스 | 명령 | 역할 |
|---|---|---|
| HTTP API | `uvicorn app.api.main:app --port 8001` | `POST /ml/ingest` → 실 Confluence crawl → Mongo `raw_pages` 적재 + RabbitMQ `content.chunking` 발행 · delta(`run_delta_sync`) · 완료 이벤트 발행 |
| Chunking Worker | `python -m app.ingestion.workers.chunking_main` | `content.chunking` 소비 → 청킹 → Dual Embedding → Qdrant Multi-Pool upsert (`[embedding]` extra 필요) |

- 분기 토글: `RAG_USE_REAL_ADAPTERS=true` (미설정 시 PoC 모드 — 기동 로그에 **WARNING** 출력, 실 Qdrant 미적재)
- RabbitMQ: `RAG_RABBITMQ_URL` (발행·소비 공용, durable 큐 자동 선언, persistent 발행)
- 연결 수명: API 는 **잡 단위** 연결(BackgroundTasks threadpool 안전), 워커는 장기 연결 + 자동 재접속(backoff)·SIGTERM 우아 종료·prefetch=1
- Confluence 자격증명(정본 = api-spec v2.6.2 §2-5): preferred 는 잡 본문 `adminUserId` 로 Worker 가 auth-server `GET /internal/auth/admin-confluence-credential` 을 조회해 **`accessToken`(OAuth)+`cloudId`+`siteUrl`** 을 얻는 경로다(§2-5 클라이언트 wiring 은 후속 — featureI-7c). 현 구현 대체 경로: 잡 본문 legacy `accessToken`/`cloudId` 또는 Settings `RAG_ATLASSIAN_*`. 둘 다 없으면 잡이 명시 FAILED

## 4c. backend-template 동기화 (2026-06-11) — site_url 단일화·§2-5 `siteUrl`·webui_link 정규화
backend-template 2026-06-11 확정(커밋 `6bfc668`·`bfef3cb`)을 반영했다:

| 항목 | 내용 |
|---|---|
| site_url 단일화 | BE DB 는 `admin_atlassian_credential.site_url` **한 곳**에만 저장(V004, db-schema §6.4). `public_site_url` 같은 별도 명칭 없음. §2-5 가 JSON `siteUrl`(camel) 로 ingestion 에 전달 — **env `RAG_ATLASSIAN_SITE_URL` = 이 값과 동일해야 한다** (`https://{site}.atlassian.net`) |
| §2-5 응답 | `{accessToken, cloudId, siteUrl, expiresAt}` — admin API Token 은 미반환(auth-server 내부 admin-key 관리 전용). v2.6.1 의 `adminApiToken`/`adminEmail` 반환 모델은 폐기 |
| webui_link 정규화 (**코드 수정**) | 종전엔 에이전트의 `_links.webui` **상대경로가 그대로** Qdrant `webui_link`/RAG `sources[].url` 에 적재됐다(FE 출처 링크 깨짐). full crawl(`app/adapters/atlassian.py`)·delta(`app/ingestion/sync.py`) 양 경로에 `normalize_webui_link`(site_url 기준 absolute) 적용. **atlassian 소스 + `RAG_ATLASSIAN_SITE_URL` 미주입이면 WARNING + 상대경로 유지** — 운영에서는 반드시 주입 |
| 하이브리드 모델 | 콘텐츠 조회 계약 = OAuth Bearer + 게이트웨이 + `Atl-Confluence-With-Admin-Key` 헤더. 단 이 조합은 BE **3단계 검증 게이트** — ML 실측(2026-06-02)은 API Token Basic+site URL 만 확인됐으므로 `RAG_ATLASSIAN_USE_ADMIN_KEY=true` 경로(검증된 구현)는 게이트 통과 시까지 보존 |

> 회귀: 변경 후 테스트 205 passed(신규 7 포함 — 정규화 헬퍼·full crawl 5 + delta 2), ruff clean. (sandbox 3.10 호환 심 기준 — 맥 3.11 에서 `./scripts/verify.sh` 재확인 권장)

## 5. 참고 문서 (원본 워크스페이스)
- `HANDOFF-ML-2026-06-09.md` — 인프라 seam·운영 기본값 §4·§5
- `SHARED-SURFACE-2026-06-09.md` — `lina-shared` 공유표면 추출(별개 작업, 미포함)
- `rag/docs/api-spec.md` (v2.6.2) — 정본 계약 (backend-template `docs/api-spec.md` 2026-06-11 자와 동기)
