# Working Log — Data Ingestion Pipeline

작업 중 내린 중요한 결정·변경 이유·부작용 가능성을 시간순으로 기록한다(루트 CLAUDE.md
"작업 중 중요한 결정은 문서에 남긴다"). 상세 Plan 은 `docs/ai/current-plan.md` 참조.

---

## 2026-06-11 — featureI-7c Step 1 완료: completion 이벤트 DTO/스키마 정합

**작업**: `IngestCompletionEvent` 스키마와 필수 필드를 API-spec v2.5.0 기준으로 정합.

**변경 범위**

- `app/api/ingest_completion.py`
  - `jobId/adminUserId/mode/status/completedAt` 중심 payload 계약 적용.
  - `status` 허용값을 `COMPLETED|FAILED`로 제한.

## 2026-06-11 — featureI-7c Step 2 완료: MQ 발행 계약 정합

**작업**: completion 발행 기본 라우팅 계약 정리.

**변경 범위**

- `app/ingestion/workers/publisher.py`
  - 기본 exchange `""`, 기본 routing key `lina.admin.ingest.completion` 정합.
  - `delivery_mode=2`, `content_type="application/json"` 적용.

## 2026-06-11 — featureI-7c Step 3 완료: ingestion worker publish 연결

**작업**: 수집 성공/실패 종료 경로에서 completion publish 호출 연결.

**변경 범위**

- `app/ingestion/workers/ingestion_worker.py`
  - full/delta 수집 종료 분기에서 COMPLETED/FAILED 이벤트 발행 보강.

## 2026-06-11 — featureI-7c Step 4 완료: 실패 경로에서 FAILED 강제 발행 보장

**작업**: 수집 실패 시 publish 보장을 강화.

**변경 범위**

- `app/ingestion/workers/ingestion_worker.py`
  - 실패 경로에서 completion publish의 선행/우선 보장 강화.

## 2026-06-11 — featureI-7c Step 5 완료: publish 실패 처리/재시도 정합

**작업**: publisher 실패 재시도/로그/민감정보 처리 정합화.

**변경 범위**

- `app/ingestion/workers/publisher.py`
  - 재시도 정책, backoff, 실패 시 민감정보 마스킹 로그 보완.

## 2026-06-11 — featureI-7c Step 6 완료: completion 큐/라우팅 설정 정비

**작업**: completion 큐/키 기본값을 config로 정비.

**변경 범위**

- `app/config.py`
  - `ingest_completion_routing_key`, completion queue/DLQ 기본값 설정 정비.

## 2026-06-11 — featureI-7c Step 7 완료: 기본 단위 테스트 정리

**작업**: worker 중심 completion 이벤트 단위 테스트 보강.

**변경 범위**

- `tests/ingestion/test_ingestion_worker.py`
  - 성공/실패 publish 상태 검증 및 민감 필드 미포함 보강.

## 2026-06-11 — featureI-7c Step 8 완료: completion 이벤트 전용 테스트 추가

**작업**: Step 8 전용 테스트 스위트 추가.

**변경 범위**

- `tests/ingestion/test_completion_events.py`(신규)
  - `job_id` 누락/`admin_user_id` 보정/비터미널 status 유효성 검증.
  - `exchange`, `routing_key`, `delivery_mode`, `content_type` 발행 속성 검증.

**검증**

- `python3 -m pytest tests/ingestion/test_completion_events.py`: 실행 환경에서 `pytest` 미설치로 보류.

**남은 리스크/후속**

- Step 9 통합 테스트는 미진행.

## 2026-06-11 — featureI-7c Step 9 진행: completion 큐 통합 경로 검증

**작업**: `tests/integration/` 통합 테스트를 추가해 실제 RabbitMQ completion 큐 적재/consume 경로 검증.

**변경 범위**

- `tests/integration/test_completion_queue_integration.py`(신규)
  - `Testcontainers` 가능 시 `RabbitMqContainer`로 RabbitMQ 기동 후 completion queue를 선언하고
    `QueueIngestCompletionPublisher`로 이벤트를 적재.
  - `basic_get` consume로 메시지 수신을 확인하고, 메시지 속성(`delivery_mode`, `content_type`) 및
    payload(`status/eventType`, jobId, 민감 필드 미포함)를 검증.
  - `COMPLETED`, `FAILED` 2종 상태를 연속 publish/consume.

**검증**

- `python3 -m pytest tests/integration/test_completion_queue_integration.py`
  실행 시 pytest는 확인되었고, `testcontainers` 설치 후에도 현재 환경은 Docker 소켓 권한(`Operation not
  permitted`)으로 컨테이너 시작이 불가해 통합 실행은 실패(실패 처리)했습니다.
- 점검 중 통합 테스트 내부 검증 문자열을 실제 메시지 값(`test integration failure`) 기준으로
  `ingest failure` → `test integration failure`로 수정했습니다.
- 변경된 통합 테스트는 Docker 권한/도커 접근 가능한 환경에서 재실행 시 실제 completion queue 적재
  경로 검증이 가능합니다.

**남은 리스크/후속**

- Testcontainers/도커/RabbitMQ 환경이 없는 경우 이 테스트는 실행 시 스킵됨.

## 2026-06-11 — Step 9 보강: 통합 테스트 검증 오류 보정

**작업**: Step 9 통합 테스트의 값 검증 실패 가능성을 제거.

**변경 범위**

- `tests/integration/test_completion_queue_integration.py`
  - FAILED payload message 검증 어서션을 `"test integration failure"`로 정합.

**검증**

- 실제 메시지 본문(`message="test integration failure"`) 기준으로 어서션을 맞춰 재확인.
- 실행은 동일 Docker 소켓 권한 제약으로 통합 컨테이너 구동 단계에서 재확인 필요.

## 2026-06-11 — Codex 작업 지침 표면 추가

**작업**: 기존 Claude Code 중심 문서 체계를 Codex에서도 바로 사용할 수 있도록 `AGENTS.md` 계층을
추가했다. 루트 `AGENTS.md`는 저장소 공통 규칙, `app/AGENTS.md`는 `app/`·`tests/` 전용 규칙을 담는다.
기존 `CLAUDE.md`와 `app/CLAUDE.md`는 Claude Code 호환을 위해 유지하고, 규칙 변경 시 두 계열을 함께
갱신하도록 상호 참조를 추가했다.

**변경 범위**

- `AGENTS.md`, `app/AGENTS.md`: Codex용 지속 지침 추가.
- `CLAUDE.md`, `app/CLAUDE.md`: Codex 문서와의 연결 문구 추가.
- `docs/ai/workflow.md`: Claude Code 전용 표현을 Codex/Claude 공용 작업 플로우로 조정.
- `README.md`: AI 작업 지침 섹션 추가.

**검증**: 문서 변경만 수행. `git diff --check` 통과.

## 2026-05-26 — ADR 0003 항목 3 운영 wiring: crawl 잡 기록 연결 (ingestion 단독)

**작업**: 항목 3에서 `crawler.run_full_crawl`에 optional `jobs` 주입을 추가했으나, in-process
조립(`pipeline.py`)에서는 crawl 에 jobs 가 연결되지 않아 CRAWL 잡이 실제로 기록되지 않았다.
crawl 과 chunking_worker 가 **동일 jobs 인스턴스를 공유**하도록 연결해 한 `ingestion_jobs` 에
CRAWL(페이지별) + UPSERT 가 함께 남도록 했다.

**변경(ingestion 단독 — 공유 자산·rag 무변경)**

- `app/ingestion/pipeline.py`:
  - `run_ingestion_pipeline` 이 `run_full_crawl(..., jobs=chunking_deps.jobs)` 로 호출 — crawl 과
    worker 가 같은 jobs 인스턴스 공유(비파괴: jobs None 이면 양쪽 모두 기록 생략).
  - `build_poc_components` 가 `FakeIngestionJobsRepository` 를 생성해 `ChunkingWorkerDeps.jobs` 에
    주입하고 `PocComponents.jobs` 로 노출(이전엔 jobs 미주입 → None 이라 PoC 에서 기록 안 됨).
- `tests/ingestion/test_pipeline_e2e.py`: PoC 전 체인 실행 후 공유 jobs 에 CRAWL(페이지별 SUCCESS)
  + UPSERT 가 함께 기록되는지 검증하는 테스트 추가.

**범위 메모**: 실 운영 경로(`bootstrap.build_chunking_worker_deps` 의 Mongo jobs)는 RabbitMQ consumer
실행 loop(featureI-7c, 인프라 의존 TBD)에서 동일하게 `run_full_crawl(jobs=deps.jobs)` 로 연결하면
된다 — 본 change-set 은 in-process 조립(PoC/테스트) wiring 까지다.

**검증**: ruff(line-length 100, 통과) + py_compile(통과). 전체 `./scripts/verify.sh` 는 Mac(3.11).
공유 자산 무변경이라 rag 영향 없음(ingestion 단독 커밋).

---

## 2026-05-26 — ADR 0003 항목 4 적용: soft_delete 도입 (승인됨)

**작업**: ADR 0003 항목 4(승인 필요로 보류했던 항목)를 사용자 승인 후 적용. Qdrant payload에
soft-delete 플래그를 도입하고, rag 검색이 삭제분을 제외하도록 공유 계약을 확장.

**변경(공유 자산 — 양 레포 동시·바이트 동일)**

- `app/ingestion/vector_store.py:build_point_payload`: `"is_deleted": False` 추가(신규/재색인
  upsert 기본값). owning source rag 먼저 → ingestion 미러.
- `app/storage/qdrant_client.py`:
  - `_BOOL_INDEX_FIELDS=("is_deleted",)` + `_ensure_payload_indexes`에 BOOL 인덱스 생성.
  - `_build_combined_filter`에 `must_not(is_deleted=true)` 추가 — 모든 검색이 삭제분 제외.
    필드 부재(legacy)는 매칭 안 돼 자연 통과(미삭제 간주, 재색인 없이 후방 호환).
  - `soft_delete_by_page_id`/`soft_delete_by_attachment_id`/`_soft_delete_by_field`(set_payload)
    추가 — Point 보존하고 `is_deleted`만 True. hard delete(`delete_by_*`)는 그대로 보존.
  - rag에서 편집 후 ingestion에 파일 단위 복사로 바이트 동일 보장.

**변경(레포 전용)**

- (ingestion) `app/storage/qdrant_fake.py`: `_StoredPoint.is_deleted` 추가 + 실 store와 동일한
  `soft_delete_by_*`(dataclasses.replace) — 드롭인 인터페이스 정합.
- (ingestion) `tests/ingestion/test_qdrant_fake.py`(신규): Fake soft_delete 가 플래그만 갱신하고
  Point 보존하는지 검증.
- (rag) `tests/storage/test_qdrant_client.py`: soft_delete 가 검색 제외 + Point 보존(count 불변),
  첨부 soft_delete, 미삭제 청크 정상 검색 테스트 추가.
- (rag) `tests/ingestion/test_vector_store.py`: payload `is_deleted` 기본 False 단언.
- 양 레포 `docs/db-schema.md` §1.2(is_deleted 행)·§1.3(bool 인덱스), `docs/adr/0003` 항목 4 상태
  "적용됨".

**영향/주의**: soft/hard delete는 호출 측 선택. **양 레포 동시 배포** 필요. 기존 인덱스의 Point는
`is_deleted` 필드가 없어 자동으로 "미삭제"로 동작하지만, 명시적으로 채우려면 재색인 또는 일괄
`set_payload` 백필이 필요하다. 삭제 트리거(Delta Sync `deleted_candidate`/Trash/Webhook →
`soft_delete_by_*`) 실배선은 store를 소유한 Sync Worker의 운영 wiring 후속(능력은 도입 완료).

**검증**: 샌드박스(3.10)는 qdrant-client/StrEnum 부재로 pytest 불가 → ruff(line-length 100, 통과) +
py_compile(통과) + 공유 자산 바이트 동일 확인. **전체 `./scripts/verify.sh`는 양 레포 Mac(3.11)에서
수행 필요**(특히 rag `:memory:` Qdrant 통합 테스트 — soft_delete 검색 제외).

---

## 2026-05-26 — ADR 0003 항목 3 적용: IngestionStage.CRAWL 추가 + crawl 잡 기록 (승인됨)

**작업**: ADR 0003 항목 3(승인 필요로 보류했던 항목)을 사용자 승인 후 적용. 공유 enum
`IngestionStage`에 수집 단계 값을 추가하고 ingestion crawl 단계 `ingestion_jobs` 기록을 배선.

**변경(공유 자산 — 양 레포 동시·바이트 동일)**

- `app/schemas/enums.py`: `IngestionStage`에 `CRAWL = "crawl"` 추가(파이프라인 순서상 ANALYZE 앞).
  owning source인 rag를 먼저 수정하고 ingestion에 동일 미러 — `diff`로 바이트 동일 확인.

**변경(레포 전용)**

- (ingestion) `app/ingestion/crawler.py`: `run_full_crawl`에 `jobs: IngestionJobsRepository | None
  = None` 추가. 주입 시 적재·발행에 성공한 페이지마다 `IngestionStage.CRAWL` + `IngestionStatus.
  SUCCESS` 기록(비파괴 — 미주입이면 기존 동작). 실패 페이지는 적합한 status 코드가 없어
  `failed_page_ids`로만 격리(잡 레코드 미기록). `datetime.now(UTC)` 사용(기존 chunking_worker 정합).
- (ingestion) `tests/ingestion/test_crawler.py`: jobs 주입 시 CRAWL SUCCESS 기록 / 실패 페이지 미기록
  테스트 2건 추가.
- (rag) `tests/schemas/test_enums.py`: `IngestionStage` 멤버셋에 `crawl` 추가(기존 동치 assert 갱신).
- 양 레포 `docs/db-schema.md` §2.3 `stage` 설명에 `crawl` 반영, `docs/adr/0003` 항목 3 상태를
  "적용됨"으로 갱신.

**영향/주의**: 공유 enum 변경이므로 **양 레포를 함께 배포**해야 한다. ingestion이 `"crawl"`을 기록한
`ingestion_jobs` 레코드를, enum 미갱신 레포(또는 대시보드)가 `IngestionStage(value)`로 역파싱하면
`ValueError`가 날 수 있다(ADR 0003 항목 3 영향). 관리자 대시보드는 별도 시스템 — stage 화이트리스트
확인 권장.

**검증**: 샌드박스(Python 3.10)는 StrEnum/venv 부재로 pytest 불가 → ruff(line-length 100, 통과) +
py_compile(통과)까지 수행. **전체 `./scripts/verify.sh`(format→lint→test)는 양 레포 Mac(3.11)에서
수행 필요**(특히 rag test_enums, ingestion test_crawler).

**후속**: ADR 0003 항목 4(soft_delete)는 별도 change-set로 이어서 진행. crawl 잡 기록의 실 배선
(bootstrap에서 `jobs` 주입)은 운영 wiring 시 연결.

---

## 2026-05-26 — ingestion↔rag 공유 계약 합의 (ADR 0003)

**작업**: ingestion·rag 두 레포가 공유하는 미해결 계약(TBD)을 식별·결정하고 ADR로 동결. 결정 결과를
양 레포 `docs/adr/0003`에 **동일 복제**하고 관련 문서를 정합 갱신. 코드 diff로 공유 자산 분기 현황을
직접 검증(추측 금지).

**검증한 현황(diff/grep)**

- 공유 자산(`app/schemas/*`, `vector_store`, `indexer`, `embedding`, `qdrant_client`, `jobs`,
  `mongo_cache`, `adapters/{base,json_fixture}`)은 rag와 **바이트 동일**. 유일 분기 = `sync.py`
  (본 레포가 `run_delta_sync` additive 추가, 공유 `reconcile_deletions`는 동일).
- `chunk_id=SHA1("{page_id}:{chunk_index}:{attachment_id}")`, cache 키=`(chunk_id, version_number)`,
  payload=`build_point_payload` — 양 레포 동일. rag 검색에 soft-delete 필터 없음(grep 확인).

**결정(상세는 ADR 0003)**

- **항목 1 ACL: (A) `space_key` 합성 확정**(ADR 0002 `space:` prefix 전제). seam =
  `synthesize_space_acl`/`_synthesize_acl`(본 레포) ↔ rag `build_acl_filter`. 런타임 무변.
- **항목 2 payload/cache/chunk_id: owning=rag**, 변경 시 양 레포 동시 + 재색인. 분기 등록부 기록.
- **항목 3 `IngestionStage`에 `CRAWL` 추가: 제안만 — 승인 필요**(공유 enum, 동시 배포 필요). crawl 잡
  기록 보류 현행 유지. enum 코드 미변경.
- **항목 4 soft_delete: PoC는 hard delete 유지**. 도입 규약(payload `is_deleted`+검색 `must_not`+재색인)
  만 기록 — **승인 필요**. `sync.py`의 `deleted_candidate` surface(미파괴)는 현행 유지.
- **항목 5 공유 자산: 복사 유지**, 분리는 분기 비용 증가 시 재검토.
- **합의 불필요**: `access_token`/`cloudId` 전달(Auth/BFF — 두 레포에 코드 없음), JWT 발급·서명,
  관리자 대시보드 데이터.

**수정 파일**: `docs/adr/0003-ingestion-rag-shared-contracts.md`(신규), `docs/db-schema.md`(§1.4 ACL·
§2.3 stage 노트), `docs/atlassian-api.md`(ACL 절 "미해결"→"결정"), 본 `working-log.md`.
**런타임 코드·공유 자산(schemas/enums/vector_store 등) 미변경** — 문서/거버넌스 정합만.

**검증**: 문서 전용 변경이라 코드/테스트 무영향. `git diff`로 `docs/` 한정 확인. 비밀정보 미포함.

**후속(승인 대기)**: 항목 3(enum `CRAWL`)·항목 4(soft_delete)는 사람 승인 후 별도 change-set로
양 레포 동시 적용. 영향·절차는 ADR 0003 "사람 승인이 필요한 항목 요약" 표 참조.

---

## 2026-05-26 — featureI-6: 외부 에이전트 2종 vendoring 통합 (FR-001 / FR-005)

**작업**: 별도 레포에서 개발된 Data Ingestion Agent·Data Sync Agent 두 패키지를 ingestion
파이프라인에 vendoring + 얇은 어댑터로 통합. featureI-2(Full Crawl)·featureI-5(Delta Sync)를
신규 작성이 아닌 외부 에이전트 통합으로 구현.

**결정**

- **vendoring 레이아웃(rag 미러)**: import 가능한 패키지를 저장소 루트로 이동
  (`data_ingestion_agent/`, `data_sync_agent/` — 코드 무수정, 위치만 이동). 에이전트의
  `scripts/`+`tests/` 는 `tests/<agent>/` 아래에 무수정 복사. 에이전트 integration test 가
  `Path(__file__).parents[N]` 로 `scripts/`·`fixtures/` 상대 경로를 참조하므로 원본
  프로젝트 구조(scripts/ + tests/)를 그대로 보존해야 통과한다. 두 에이전트의 동일 테스트
  파일명(test_schema_config.py 등) 충돌을 막기 위해 vendored 테스트 디렉토리에 pytest
  패키지 마커 `__init__.py` 만 추가(브리프 허용 범위).
- **어댑터 구동 = 상단 workflow 블랙박스 호출**: `run_full_crawl_workflow` /
  `run_data_sync_workflow` 를 in-process 로 호출하고 산출물(`.documents`/`.changed_documents`)
  을 메모리에서 `PageObject` 로 변환. 에이전트가 로컬 파일로 쓰는 산출물은 임시 디렉토리로
  우회 후 즉시 정리(파이프라인은 MongoDB `raw_pages` 적재 + Chunking Queue 발행).
- **pyproject**: `packages.find.include` 에 `data_ingestion_agent*`/`data_sync_agent*` 추가,
  `[tool.ruff] extend-exclude` + `[tool.mypy] exclude` 에 vendored 패키지·테스트 경로 추가
  (원본을 우리 컨벤션 line-length 100 등에 맞추지 않고 무수정 보존). langgraph 는 선택
  의존성(`agents` extra) — 미설치 시 두 에이전트 모두 sequential fallback.

**계획 대비 실측 차이(에이전트 MVP)**

- 에이전트는 자체 `ProcessedDocument`(중첩 space/page/body/metadata) 스키마를 산출 →
  어댑터가 평면 `PageObject` 로 변환(`body_html←body.storage_html`,
  `last_modified←page.last_modified_at`, `webui_link←page.page_url` 등).
- ACL / labels / ancestors / 첨부(not_supported_in_mvp) 미산출 → ACL 은 space_key 합성
  (`synthesize_space_acl`, JsonFixture PoC 패턴 동일), labels/ancestors/attachments 는 빈 값.
- `IngestionStage` enum 에 crawl/ingest 단계가 없어(공유 자산 — RAG `ingestion_jobs` 대시보드와
  계약 공유) crawl 단계 `ingestion_jobs` 기록은 본 change-set 에서 보류. `CrawlResult`/
  `DeltaSyncResult` 를 잡 리포트로 사용. enum 추가는 RAG 분기 영향 설명 후 별도 협의.

**미해결(추측 구현 금지 — current-plan.md featureI-6 TBD)**

- ACL 실연동(space_key vs content restrictions) — RAG 검색 ACL 필터와 공유 계약.
- `access_token`/`cloud_id` 전달 경로(Auth Server→BFF→Ingestion) — PoC placeholder(Settings
  env 주입 / CrawlRequest·DeltaSyncRequest 주입) 유지.
- 첨부 수집·추출(FR-002, featureI-3), Trash API/Webhook 삭제(에이전트·본 레포 모두 MVP 제외),
  삭제 후보 Qdrant soft_delete 실행(store 소유 Worker 책임), snapshot Mongo 영속화.

**검증**

- 샌드박스(Python 3.10)는 vendored 의 `enum.StrEnum`(3.11+) import 불가 + 의존성 미설치 →
  여기서는 ruff(app·tests 통과) + `py_compile`(vendored 포함 전체 syntax OK)까지만 수행.
- 전체 pytest·`./scripts/verify.sh`·git push 는 Mac(3.11)에서 수행 필요.

**보안**: access_token 은 Settings `SecretStr`/주입 인자로만 다루고 로그·메시지 페이로드·
테스트 픽스처에 남기지 않음(에이전트 자체 redaction + 어댑터 placeholder).

---

## 2026-05-26 — featureI-4: Chunking+Embedding Worker 배선 (FR-003 / FR-004)

**작업**: featureI-6로 연결된 앞 절반(crawl→raw_pages→`content.chunking` 발행)을 이어받아,
`content.chunking` 메시지를 소비해 Adaptive Chunker → Dual Embedding → Qdrant upsert 까지
배선. 끊겨 있던 큐 소비 단계를 채워 수집 파이프라인을 end-to-end로 연결.

**결정**

- **단일 Worker 토폴로지(A)**: 복사된 `indexer.index_chunks` 가 embed+upsert+cache 를 결합하므로
  하나의 Worker 가 `content.chunking` 소비 → `raw_pages.get_page` → `chunk_page` → `index_chunks`
  를 수행한다. `content.embedding` 큐는 상수로 예약(운영 스케일링 시 2-Worker 분리 여지).
- **doc_type 폴백 우선**: `chunk_page(page)` 의 라벨 휴리스틱(`infer_doc_type`, 미매칭 시
  operation) 사용. GPT-4o-mini 문서 분석기[Agent] + MySQL `space_doc_type_cache` 는 featureI-4b 후속.
- **의존성 주입**: 임베더/Qdrant/cache/raw_store/jobs 를 `ChunkingWorkerDeps` 로 주입(테스트는 Fake).
  실 어댑터(E5/BM25/Qdrant from_settings) 부트스트랩은 배포 wiring(후속).
- **잡 기록**: 단일 Worker 라 색인 종단 단계인 `IngestionStage.UPSERT` 로 1건 기록(SUCCESS /
  INVALID_ACL / EMPTY_BODY — 모두 기존 enum 값). crawl 단계와 달리 stage enum 이 존재해 기록 가능.

**게이트(app/CLAUDE.md §3 정합)**: ACL 누락 페이지(`is_acl_missing`)는 색인하지 않고 INVALID_ACL,
청크 0건은 EMPTY_BODY, `page_id` 가 raw_pages 에 없으면 `RawPageNotFoundError`(상위 DLQ).

**신규/수정 파일**: `app/ingestion/workers/consumer.py`(MessageConsumer ABC+Fake+Pika),
`app/ingestion/workers/chunking_worker.py`(process_chunking_message + run_chunking_worker),
`app/storage/raw_store.py`(`get_page` 읽기 추가 — Fake/Mongo), `tests/ingestion/test_chunking_worker.py`,
`docs/architecture.md`·`docs/ai/current-plan.md`.

**검증**: ruff check/format + mypy app(41 files) 통과(샌드박스). 멱등성(동일 version 재실행 skip)·
ACL/빈 본문 게이트·잡 기록·end-to-end 를 Fake(임베더/Qdrant/cache)로 테스트. 전체 pytest·
`./scripts/verify.sh`·push 는 Mac(3.11)에서.

**후속(TBD)**: featureI-4b(GPT-4o-mini 문서 분석기 + space_doc_type_cache), 첨부 청크 경로
(FR-002 첨부 입력 생성 후), 실 어댑터 부트스트랩 + pika consumer 배포 wiring, `content.embedding`
2-Worker 분리(운영 스케일링 시).

---

## 2026-05-26 — featureI-4b: 문서 분석기 [Agent] + MySQL space_doc_type_cache (FR-003)

**작업**: featureI-4 의 라벨 휴리스틱 폴백을 대체해, 스페이스 단위 1회 GPT-4o-mini doc_type
판별 → MySQL `space_doc_type_cache` 캐싱 → 이후 같은 스페이스 페이지는 캐시 재사용. Chunking
Worker 에 optional resolver 로 연결.

**결정**

- **LLM 격리(Agent)**: `DocTypeClassifier` ABC + `FakeDocTypeClassifier` + `OpenAIDocTypeClassifier`
  (GPT-4o-mini, **Function Calling 으로 스키마 강제**, 타임아웃). 비결정론 LLM 을 어댑터 경계에
  격리해 테스트는 Fake 로 대체(app/CLAUDE.md §5).
- **폴백**: 신뢰도 < 0.6 또는 LLM 실패 시 `DocType.OPERATION`. DocType enum 에 'general' 이 없어
  (chunker 폴백·db-schema confidence 주석과 정합) operation 사용 — CLAUDE.md §FR-003 의 'general'
  표기와의 차이는 본 로그에 명시. **저신뢰는 캐싱**(반복 호출 방지), **일시적 LLM 실패는 미캐싱**
  (다음 페이지 재시도).
- **스페이스 1회 판별**: 캐시 우선. 미스 시 현재 페이지 1샘플(title+labels+body 일부)로 분류 후
  캐싱(sample_count=1). 다중 샘플 스페이스 분석은 TBD.
- **Worker 연동(비파괴)**: `ChunkingWorkerDeps.doc_type_resolver`(optional) 추가. 주입 시
  `chunk_page(page, resolver.resolve_doc_type(page))`, 미주입 시 기존 라벨 폴백 — featureI-4 동작 무변.

**신규/수정 파일**: `app/storage/space_doc_type_cache.py`(ABC+Fake+MySQL, db-schema §3.1),
`app/ingestion/document_analyzer.py`(분석기[Agent]), `app/ingestion/workers/chunking_worker.py`
(resolver 연동), `app/storage/__init__.py`(export), `tests/ingestion/test_document_analyzer.py`,
`docs/architecture.md`·`docs/ai/current-plan.md`.

**검증**: ruff check/format + mypy app(43 files) 통과. 캐시 미스→분류→캐싱, 캐시 히트 재사용(LLM
재호출 없음), 저신뢰/예외→OPERATION 폴백, Worker 가 resolver doc_type(incident)으로 청킹을 Fake 로
테스트. 전체 pytest·verify.sh·push 는 Mac(3.11).

**후속(TBD)**: 다중 샘플 스페이스 분석, 실 OpenAI/MySQL 부트스트랩 + Worker 에 resolver 주입 배포
wiring, 첨부 분석기(attachment_analyzer) 연동(FR-002 이후).

---

## 2026-05-26 — featureI-3: 첨부 텍스트 추출기 코어 (FR-002)

**작업**: 첨부 바이너리(PDF/Word/Excel/CSV) → 텍스트 추출 결정론 Pipeline 구현. 이미지·도형 제외,
Excel/CSV 는 시트→자연어 직렬화. self-contained(공급원 무관, bytes→text).

**스코프 결정**: vendored 에이전트 MVP 가 첨부를 수집하지 않아(`not_supported_in_mvp`)
`raw_attachments` 입력이 없으므로, 이번엔 **추출기 코어 + 단위 테스트**만 구현. 첨부 수집기
(Confluence Attachment API 다운로드 → `raw_attachments`)·`attachment_texts` 적재·Attachment/Chunking
Queue 배선·chunker `chunk_attachment` 연결은 후속(featureI-3b). 수집은 에이전트/어댑터 확장 선행 필요.

**구현**

- `extractor/pdf.py` — PyMuPDF(fitz) 1차 → 예외/빈 결과 시 pdfplumber 폴백. `RAW_TEXT`.
- `extractor/docx.py` — python-docx 문단 + 표(행 `cell | cell`). `RAW_TEXT`.
- `extractor/spreadsheet.py` — openpyxl(xlsx)/csv → `Sheet: <name>` + `헤더: 값` 직렬화. `SHEET_SERIALIZED`.
- `extractor/base.py`(stub→구현) — 유형 디스패치 + **graceful degrade**(예외 → `ok=False` + reason).
  라이브러리는 각 모듈 함수 내 **지연 import**(app import 가 extras 미설치에서도 동작).

**보안**: 실패 reason 에는 **예외 타입명만** 남기고 첨부 내용·자격증명을 포함하지 않는다.

**검증**: ruff/format + mypy app(extras 설치해 Mac 재현, 46 files) 통과. 추출 4유형은 샌드박스
(Python 3.10)에서 모듈 standalone 로드(StrEnum 미경유)로 실제 라이브러리 구동 스모크 통과
(DOCX 문단+표 / XLSX·CSV 직렬화 / PDF 텍스트 / 손상 PDF → graceful degrade). 전체 pytest 는 Mac.
`tests/ingestion/test_attachment_extractor.py`(in-test 파일 생성, 미설치 라이브러리는 importorskip),
`tests/test_scaffold.py` 추출기 stub 테스트를 구현 계약(CSV=stdlib) 검증으로 갱신.

**후속(featureI-3b TBD)**: 첨부 수집기(다운로드→raw_attachments), `attachment_texts` 적재,
Attachment/Chunking Queue(첨부) 배선, chunker `chunk_attachment` 경로 연결.

---

## 2026-05-26 — featureI-7: 파이프라인 조립(composition) + in-process end-to-end PoC

**작업**: featureI-6(crawl→raw_pages→`content.chunking`)·featureI-4(chunking_worker→Qdrant)로
나뉜 두 절반을 in-process 로 합성해 전 체인 end-to-end 동작을 검증. 재사용 가능한
`FakeQdrantPoolStore`(PoC 모드 enabler)도 신설.

**구현**

- `app/storage/qdrant_fake.py` — `FakeQdrantPoolStore`(in-memory). `upsert_chunks_batch`/
  `scroll_page_ids`/`scroll_attachment_ids`/`delete_by_*` 를 `QdrantPoolStore` 와 호환 구현.
  **공유 `qdrant_client.py` 무수정**(additive 새 모듈). 검색(search)은 Query 단계 책임이라 미구현.
- `app/ingestion/pipeline.py` — `run_ingestion_pipeline`(crawl → 발행 `content.chunking` 메시지
  in-process drain → chunking_worker) + `build_poc_components`(all-fakes, **raw_store 를 crawl·worker
  공용 인스턴스로 공유**) + `run_poc_ingestion` 편의 함수. `PipelineResult`/`PocComponents`.

**설계 메모**: 본 합성은 **PoC/테스트용**이다(운영은 crawl·chunking_worker 를 RabbitMQ 로 분리해
독립 스케일링 — featureI-7b 배포 wiring). `run_ingestion_pipeline` 은 발행 메시지를 in-process 로
drain 하므로 `FakeQueuePublisher` 전제. raw_store 공유가 핵심(메시지엔 page_id 만, 본문은 raw_store 로드).

**검증**: ruff/format + mypy app(48 files) 통과. end-to-end 테스트 `tests/ingestion/test_pipeline_e2e.py`
— ① crawl→raw_pages→발행→worker→FakeQdrantPoolStore 적재(scroll_page_ids 검증), ② 동일 raw_store·
cache·store 공유 재실행 멱등성(재upsert 스킵), ③ ACL 누락 페이지 색인 차단 전파. 전체 pytest 는 Mac.

**후속(featureI-7b TBD)**: 실 어댑터(E5/BM25/Qdrant/Mongo from_settings) 부트스트랩 + pika consumer
실행 loop + CLI 엔트리포인트(인프라 의존 — 통합 환경 검증).

---

## 2026-05-26 — featureI-7b: 의존성 부트스트랩(composition root) (FR-003/FR-004 조립)

**작업**: Worker·crawl 이 쓰는 외부 의존성을 `Settings.use_real_adapters` 토글로 PoC(전부 Fake) 또는
실 어댑터로 조립하는 composition root. config.py 의 use_real_adapters 패턴 재사용.

**구현**: `app/ingestion/bootstrap.py`
- `build_raw_page_store(settings)` — PoC `FakeRawPageStore` / 실 `MongoRawPageStore.from_settings`.
- `build_document_analyzer(settings)` — PoC `None`(chunk_page 라벨 폴백) / 실 `DocumentAnalyzer`
  (`OpenAIDocTypeClassifier` + `MySQLSpaceDocTypeCache`).
- `build_chunking_worker_deps(settings, *, raw_store=None)` — PoC Fake 전부(FakeQdrantPoolStore 등) /
  실 E5+BM25+Qdrant.from_settings+Mongo cache/jobs+분석기. `raw_store` 주입 시 crawl·worker 공유
  (in-process PoC). 실 어댑터는 **함수 내 지연 import**(torch/qdrant/openai 를 실행 시점으로 미룸).

**검증**: ruff/format + mypy app(49 files) 통과. PoC 모드 빌더(Fake 반환·raw_store 공유) 단위 테스트
`tests/ingestion/test_bootstrap.py`. 실 어댑터 모드는 인프라 의존이라 통합 환경에서 검증.

**후속(featureI-7c TBD)**: pika consumer/publisher 실행 loop + CLI 엔트리포인트(RabbitMQ 연결).
`Settings` 에 `rabbitmq_url` 추가 필요.

---

## 2026-05-26 — featureI-3b: 첨부 청킹 체인 배선 (FR-002 → 청킹 경로 연결)

**작업**: featureI-3(추출기 코어)에서 보류했던 첨부 **청킹 체인**을 배선했다. 추출기 코어는
이미 있었고, 비어 있던 것은 첨부가 청크가 되어 Qdrant 에 적재되는 경로였다.

**설계 결정(사용자 승인)**: 첨부 청킹은 **rag 레포 ingestion 그래프와 동일하게 파일 기반
`chunk_attachment`** 로 처리한다(파일을 직접 읽어 청크 생성). 별도 `attachment_texts` 컬렉션은
청킹 경로에 두지 않고, `extracted_text` 는 `raw_attachments` 에 함께 보존한다
(`analyze_attachment` 의 길이·반복비율 유효성 게이트 입력으로만 사용). 작업 범위는 **Fake 로
검증 가능한 전부**이며, 실 Confluence 다운로드 어댑터와 pika 실행 loop 는 인프라 의존 후속.

**구현**

- `app/storage/raw_store.py` — `get_attachment(attachment_id)` 읽기 메서드(ABC/Fake/Mongo).
  본문(`get_page`)과 대칭. Mongo 는 `projection={"_id": 0}` + `Attachment.model_validate`.
- `app/ingestion/workers/chunking_worker.py` — `process_chunking_message` 가 `source_type`
  으로 본문/첨부 분기(기본 `page`, 회귀 무영향). `_process_attachment_message`: raw_pages/
  raw_attachments 로드 → **부모 페이지 ACL 상속 게이트**(INVALID_ACL) → `analyze_attachment`
  (미통과 시 그 status 를 stage=ANALYZE 로 기록 후 스킵) → `chunk_attachment_fn`(ValueError 는
  `ATTACH_ENCRYPTED`/`UNSUPPORTED_ATTACH_TYPE` 로 매핑, stage=CHUNK) → `index_chunks`
  (`attachment_download_urls={id: download_url}`) → UPSERT SUCCESS. `chunk_attachment` 는 파일
  시스템 의존이라 `ChunkingWorkerDeps.chunk_attachment_fn` 으로 주입 가능(rag
  IngestionGraphDeps 패턴 정합). `AttachmentNotFoundError`, `ChunkingMessageResult.attachment_id`,
  `_record(stage, attachment_id)` 확장 추가.
- `app/ingestion/crawler.py` — `build_attachment_chunking_message(page, attachment)` +
  `run_full_crawl` 이 `page.attachments` 를 `save_attachment` + 첨부 `content.chunking`
  (`source_type=attachment`) 발행. 첨부 단위 적재·발행 실패는 `failed_attachment_ids` 로 격리
  (페이지·다른 첨부 무영향), `jobs` 주입 시 첨부 CRAWL SUCCESS 기록.
- `app/ingestion/pipeline.py` — `build_poc_components`/`run_poc_ingestion` 에 `chunk_attachment_fn`
  주입 파라미터(파일 시스템 없이 crawl→첨부 적재→첨부 청킹→Qdrant 전 체인 e2e).

**설계 메모**

- 첨부 메시지는 **본문과 같은 `content.chunking` 큐**를 공유하고 `source_type` 으로만 구분한다
  (신규 큐 미추가 — `QUEUE_ATTACHMENT` 는 예약 유지). 단일 Worker 가 양쪽을 소비한다.
- 첨부 청크의 멱등성은 본문과 동일하게 `(chunk_id, version_number)` 캐시로 보장된다
  (`version_by_page_id={page.page_id: page.version_number}`). 첨부 `chunk_id` 는
  `make_chunk_id(page_id, chunk_index, attachment_id)` 로 결정론적.
- ACL: 첨부는 부모 페이지 ACL 을 상속하므로(`build_attachment_metadata`), 부모가 INVALID_ACL
  이면 첨부도 색인하지 않는다.

**검증**: ruff check / ruff format / py_compile(app 전체 + 변경 테스트) 통과. 첨부 청킹 테스트는
`chunk_attachment_fn` fake 주입으로 외부 파일 의존성을 회피한다. 신규/확장 테스트:
`test_chunking_worker.py`(첨부 청킹·ACL 상속·미지원/저품질→ANALYZE·ATTACH_ENCRYPTED/기타
ValueError→CHUNK·멱등성·누락 첨부/부모 페이지·본문+첨부 혼합 디스패치),
`test_crawler.py`(첨부 적재·발행 메시지 형식·첨부 CRAWL 잡·실패 격리),
`test_pipeline_e2e.py`(첨부 전 체인 + 재실행 멱등성), `test_raw_store.py`(get_attachment).
**전체 pytest·`./scripts/verify.sh` 는 Mac/3.11** (샌드박스 Python 3.10 은 StrEnum 미지원).

**후속(TBD)**: ① 실 Confluence 첨부 **다운로드 어댑터**(Attachment API 바이너리 수집 →
`local_path`/`extracted_text` 채움, 인프라 의존). ② pika consumer/publisher 실행 loop
(featureI-7c 와 공통 — RabbitMQ 연결). vendored 에이전트 MVP 가 첨부를 수집하면 본 체인이
그대로 동작한다(현재 `attachments=[]`).

---

## 2026-06-04 — featureI-5b: 3중 삭제 동기화 트리거 배선 (soft_delete wiring, FR-005)

**작업**: ADR 0003 항목 4로 능력만 도입돼 있던 soft_delete(`store.soft_delete_by_*`)에 실제
**트리거 3종**(Delta `deleted_candidate` / Confluence Trash API / 실시간 Webhook)을 배선했다. 세
경로가 단일 funnel `apply_soft_deletes` 로 수렴해 Qdrant payload `is_deleted=true` 를 set 한다.

**구현(신규)**

- `app/ingestion/soft_delete.py` — `SoftDeleteStore` Protocol + `SoftDeleteResult` + `apply_soft_deletes`
  (dedup·정렬·id별 try/except 격리 → 부분 성공 + 실패 목록). 외부 의존성 0.
- `app/ingestion/workers/sync_worker.py` — `SyncWorker`(store 소유): `apply_delta_deletions(confirm=)`
  (**확인 게이트 보존** — 기본 미적용, confirm 시에만 후보 soft_delete), `run_trash_sync`,
  `handle_webhook_event`. `WebhookDeleteEvent` 값 객체.
- `app/adapters/confluence_trash.py` — `TrashedIds`/`TrashSource`/`FakeTrashSource` +
  `parse_trashed_content`(순수) + `ConfluenceTrashSource`(스페이스별 `_links.next` 페이지네이션) +
  `ConfluenceTrashContentClient`(실 urllib, transport 주입). vendored 무수정 — 본 어댑터가 직접 호출.
- `app/api/webhook_routes.py` — `POST /ml/confluence/webhook` + `parse_confluence_delete_event`
  (인식된 삭제 이벤트만 처리, 비삭제·형식오류는 no-op). 인증은 BFF 책임(미들웨어 미추가).

**구현(수정)**

- `app/ingestion/bootstrap.py` — `build_soft_delete_store`(PoC Fake / 실 Qdrant, set_payload 전용).
- `app/api/deps.py` — `IngestDeps.sync_worker` 추가 + `build_ingest_deps` 조립(HTTP 는 webhook 만 쓰므로
  trash_source 미주입). `app/api/main.py` — webhook 라우터 include.
- `tests/api/test_ingest_route.py` — `IngestDeps` 신규 필드 반영(stub deps 에 sync_worker 추가).

**설계 메모**

- **확인 게이트**: `run_delta_sync` 의 surface-only 정책을 보존한다 — `apply_delta_deletions` 는 기본
  `confirm=False` 면 무적용. 자동 confirm 여부는 운영 합의 후 토글(미정).
- **Reconciliation 무수정**: 주1회 ghost 제거(`sync.reconcile_deletions`)는 hard delete 유지(직교).
- **PoC 한계**: PoC 모드의 HTTP ingest 합성 파이프라인은 자체 내부 Fake store 를 쓰므로 webhook
  soft-delete store 와 분리된다(데모상 no-op 가능). 실 어댑터(`use_real_adapters=True`)는 공유 Qdrant 정상.

**검증**: ruff check/format(12파일 clean) + py_compile 전 파일 통과. 신규 4모듈(soft_delete/
confluence_trash/sync_worker/webhook parser)은 격리 실행으로 로직 검증(funnel 격리·페이지네이션·확인
게이트·`is_deleted` set·파서 이벤트 분류). **전체 pytest·`./scripts/verify.sh` 는 Mac/3.11**(샌드박스
Python 3.10 + qdrant/fastapi 미설치). 신규/확장 테스트: `test_soft_delete.py`, `test_sync_worker.py`
(실 `FakeQdrantPoolStore` is_deleted 확인), `test_confluence_trash.py`, `test_webhook_route.py`(라우트 e2e).

**후속(TBD)**: 주기 Trash 동기화 스케줄러 + webhook 실엔드포인트 배포 + pika 실행 loop(featureI-7c
인프라 의존). Delta 확인 게이트의 운영 자동화 정책 합의.

## 2026-06-04 — api-spec v2.4.0 정합: `/ml/ingest` 에서 spaceKey 제거

사용자가 전달한 LINA API Spec v2.4.0 §2-2 정합 — 수집 요청에서 스페이스 스코프 파라미터를
제거했다. admin Key 로 admin 이 접근 가능한 **전체 스페이스**를 ML 이 iterate 하며 수집한다
(2026-06-04 결정, `/api/admin/ingest` 와 동일 모델).

**변경**

- `app/api/routes.py` — `IngestRequest` 에서 `space_key`(alias `spaceKey`, 기존 Required) 필드
  **제거**. 요청 본문은 `mode`/`accessToken`/`cloudId` 만. `ingest_route` 는 `CrawlRequest` 를
  space_key 없이 구성(전체 스페이스). 모듈 changelog 보강.
- `app/ingestion/crawler.py` — `CrawlRequest.space_key` 를 Required → **optional(기본 `""`)**.
  빈 값이면 `run_full_crawl` 의 space_key 필터가 적용되지 않아 어댑터가 넘기는 전체 페이지를
  수집한다(= 전체 스페이스). 값이 있으면 단일 스페이스로 좁힘(스케줄러 내부 용도 — API 미노출).
- 테스트 — `tests/api/test_ingest_route.py`: 요청 payload 에서 `spaceKey` 제거(`{"mode":"full"}`/
  `{}`), 빈 본문 200 회귀 추가. 기존 `CrawlRequest(space_key="ENG")` 호출(crawler/pipeline 테스트)은
  명시 인자라 무변경.

**검증**: repo 전체 `ruff check .` + format + py_compile 통과. 실 `IngestRequest` 격리 exec 로
명세 페이로드(`mode`/`accessToken`/`cloudId`) 검증 + spaceKey 부재·빈 본문·bad mode 422 확인.
`CrawlRequest()` 기본 `space_key=""`(전체) 확인. 전체 pytest·`./scripts/verify.sh` 는 Mac/3.11.

## 2026-06-09 — api-spec v2.5.0 정합: 수집 완료 이벤트(RabbitMQ completion event)

ai-agent 담당자의 최신 작업(api-spec v2.5.0)을 본 레포에 반영했다. Admin Key 말소 트리거를 BFF
polling watcher / ML→BFF HTTP callback 에서 **RabbitMQ completion event** 로 전환한다. ML/Data
Ingestion 은 credential 없는 completion event 만 발행하고, BFF consumer 가 이를 consume 해
auth-server deactivate 내부 API 를 호출한다(ML 은 Admin Key 를 직접 말소하지 않음 — 책임 분리).
본 레포는 admin_key_revoke(HTTP callback) 단계를 거치지 않았으므로, **추가 전용(additive)** 으로
completion event seam 만 도입한다(제거 대상 없음).

**변경**

- `app/api/ingest_completion.py` (신규) — `IngestCompletionEvent`(payload: `jobId`/`adminUserId`/
  `mode`/`status`/`completedAt`/`errorCode`/`message`) + publisher seam(`IngestCompletionPublisher`
  Protocol / `NoopIngestCompletionPublisher` / `QueueIngestCompletionPublisher`) +
  `publish_ingest_completion_safely`(발행 실패 격리). payload 에 `accessToken`/`refreshToken`/
  `cloudId` credential set 은 의도적으로 제외(루트 CLAUDE.md 보안 규칙).
- `app/api/routes.py` — `IngestRequest` 에 `adminUserId`(preferred 식별자) 추가, `accessToken`/
  `cloudId` 는 legacy PoC 호환 필드로 description 정정. `_run_ingest_job` 이 `mode` 를 받아
  terminal(COMPLETED/FAILED) 직후 `_publish_ingest_completion` 으로 completion event 발행.
  `ingest_route` 가 `adminUserId` 를 `CrawlRequest` 로 전달.
- `app/api/deps.py` — `IngestDeps.completion_publisher`(기본 None / 운영 wiring 전 Noop) 추가,
  `build_ingest_deps` 가 `NoopIngestCompletionPublisher()` 주입.
- `app/config.py` — `ingest_completion_routing_key="ingestion.completed"` 추가(credential 미포함
  payload 계약).
- `app/ingestion/crawler.py`·`app/ingestion/sync.py` — `CrawlRequest`/`DeltaSyncRequest` 에
  `admin_user_id`(credential 아님) 추가.
- 테스트 — `tests/api/test_ingest_completion.py`(신규: payload credential 미포함 + 라우팅 키 +
  Noop/실패 격리), `tests/api/test_ingest_route.py`(완료/실패 시 completion event 발행 + credential
  미포함 + publisher 미주입 회귀).

**남은 운영 wiring(후속)**: `QueueIngestCompletionPublisher` 를 실 RabbitMQ connection/channel 에
연결하는 worker/infra 진입점, Data Ingestion Worker 의 `adminUserId` → auth-server 내부 credential
조회 client, BFF completion event consumer 의 idempotency/retry/DLQ 정책(featureI-7c 인프라 의존).

**검증**: 변경 8파일 `ruff format`/`ruff check`(All checks passed) + `mypy --python-version 3.11`
(no issues) + py_compile 통과. 전체 `pytest`·`./scripts/verify.sh` 는 Mac/3.11 환경에서 수행한다
(샌드박스 Linux 에 3.11 인터프리터 부재).

## 2026-06-09 — FR-005 delta 수집 라우팅 (`/ml/ingest` mode=delta → Delta Sync)

`/ml/ingest` 가 `mode` 를 검증만 하고 full·delta 둘 다 full-crawl 합성으로 처리하던 갭을 해소했다.
이제 `mode=delta` 는 vendored Data Sync Agent 래퍼(`run_delta_sync`)로 분기한다. delta 실행 함수
(`app/ingestion/sync.py:run_delta_sync`)는 이미 완결돼 있어 그대로 사용한다(무수정).

**변경**

- `app/config.py` — `data_sync_previous_snapshot` 설정 추가(Delta Sync 이전 스냅샷 경로).
- `app/api/deps.py` — `IngestDeps` 에 `run_delta`(`Callable[[DeltaSyncRequest], DeltaSyncResult]`)
  + `previous_snapshot_path` 추가. 기본 delta 러너는 PoC 안전(변경분 없음, `_poc_empty_delta`)이며,
  운영 delta(`run_delta_sync` 실 client/snapshot)는 infra/worker 진입점에서 주입한다
  (`completion_publisher` 와 동일 패턴).
- `app/api/routes.py` — `ingest_route` 가 `mode=="delta"` 분기 → `_run_delta_ingest_job`
  (IN_PROGRESS → `deps.run_delta` → COMPLETED/FAILED) 백그라운드 실행. 상태 카운트는
  `processed=changed_pages` / `failed=failed_items` / `total=합`. terminal 에서 completion
  event(mode="delta") 발행. 삭제 후보(`deleted_candidate_page_ids`)의 soft-delete 실적용은
  SyncWorker/스케줄러 책임이라 본 잡은 카운트만 보고한다(범위 밖 — featureI-7c).
- `tests/api/test_ingest_route.py` — delta 회귀 2건(완료 카운트 + completion event; 실패 →
  FAILED) 추가. full 실패 회귀는 `mode=full` 로 정정.

**범위 밖**: 삭제 후보 soft-delete 실적용·주기 스케줄러(featureI-7c), ACL/소스 운영 기본값 전환.

**검증**: 변경 4파일 `ruff format`/`ruff check`(All checks passed) + `mypy app`(59 files, no issues)
+ py_compile 통과. 전체 `pytest`·`./scripts/verify.sh` 는 Mac/3.11(샌드박스 3.11 부재).

## 2026-06-09 — FR-005 delta 삭제 후보 soft-delete 적용 (확인 게이트)

delta 잡이 산출한 삭제 후보(`deleted_candidate_page_ids`)를 `SyncWorker.apply_delta_deletions` 로
실제 soft-delete 적용하도록 배선했다. **확인 게이트 보존**: 설정 `data_sync_delta_delete_confirm`
(기본 False)이 OFF면 후보만 surface(자동 삭제 안 함 — sync false-positive 로 유효 문서 삭제 방지),
True면 적용한다.

**변경**

- `app/config.py` — `data_sync_delta_delete_confirm: bool = False`(opt-in 게이트).
- `app/api/deps.py` — `IngestDeps.delta_delete_confirm` 추가, `build_ingest_deps` 가 설정에서 주입.
- `app/api/routes.py` — `_run_delta_ingest_job` 가 run_delta 성공 후
  `deps.sync_worker.apply_delta_deletions(result, confirm=deps.delta_delete_confirm)` 호출. 결과는
  로깅(부분 실패 warning). soft-delete 실패는 id 단위로 격리돼 수집 잡을 FAILED 로 만들지 않는다
  (best-effort). status/응답/completion event 계약 무변경.
- `tests/api/test_ingest_route.py` — 통합 회귀 2건(confirm=True→후보 soft-delete / confirm=False→미적용).

**PoC 한계**: `build_soft_delete_store` 가 ingest 합성 파이프라인 store 와 분리돼, PoC 데모에선 실
색인 데이터에 반영이 안 될 수 있다(운영 실 Qdrant 에선 동일 store). 단위/통합 테스트는 store 를 직접
주입해 적용을 검증한다.

**범위 밖**: Trash/Webhook 주기 실행 스케줄러(featureI-7c).

**검증**: 변경 4파일 `ruff format`/`ruff check`(All checks passed) + `mypy app`(59 files, no issues)
+ py_compile 통과. 전체 `pytest`·`./scripts/verify.sh` 는 Mac/3.11(샌드박스 3.11 부재).

## 2026-06-09 — FR-002 첨부 다운로더 (download_url → local_path seam)

`chunk_attachment` 는 첨부 파일을 파일 시스템에서 직접 읽으므로 `local_path` 가 필요한데, 운영 어댑터는
`download_url` 만 제공한다(코드 주석이 명시한 "다운로드 헬퍼가 local_path 를 채우는 정공법"). 그 누락
헬퍼를 추가했다.

**변경**

- `app/ingestion/attachment_downloader.py`(신규) — `AttachmentDownloader` Protocol +
  `NoopAttachmentDownloader`(기본) + `HttpAttachmentDownloader`(httpx 주입형 client; download_url→
  local_path, `local_path`/`file://` 는 네트워크 없이 통과) + `AttachmentDownloadError`(운영성 오류).
- `app/config.py` — `attachment_download_dir`(다운로드 저장 경로).
- `app/ingestion/workers/chunking_worker.py` — `ChunkingWorkerDeps.attachment_downloader`(기본 None)
  추가; `_process_attachment_message` 가 `chunk_attachment` 전에 `ensure_local` 로 local_path 를
  채운다. 다운로드 실패는 `AttachmentDownloadError` 로 전파 → 상위 consumer 가 재시도/DLQ
  (RawPageNotFoundError 와 동일 정책; 격리 status 로 삼키지 않음).
- `tests/ingestion/test_attachment_downloader.py`(신규) — 다운로더 단위(Noop/already-local/file:///
  http fetch/HTTP 오류) + 배선 통합(주입 시 chunk_attachment 전 local_path 채움 / 미주입 시 생략).

**범위 밖(후속)**: 실 `atlassian.py` 어댑터가 첨부 메타(download_url)를 산출하도록 하는 작업(vendored
경계 — 현재 `attachments=[]`). 라이브 end-to-end 는 이 후속과 합쳐져야 동작한다. 운영
`HttpAttachmentDownloader` wiring(자격증명 헤더)도 infra 진입점 후속.

**검증**: 변경 4파일 `ruff format`/`ruff check`(All checks passed) + `mypy app`(60 files, no issues)
+ py_compile 통과. 전체 `pytest`·`./scripts/verify.sh` 는 Mac/3.11(샌드박스 3.11 부재).

## 2026-06-09 — FR-002 첨부 다운로더 bootstrap 배선

`build_chunking_worker_deps` 실(real) branch 에 `HttpAttachmentDownloader` 를 주입했다. 인증은 기존
Confluence 클라이언트(`ConfluenceTrashContentClient`·`ConfluenceRestrictionAclProvider`)와 동일
패턴(settings `Bearer access_token` + 선택 `Atl-Confluence-With-Admin-Key` header)을 따른다(추측 아님).

**변경**

- `app/ingestion/bootstrap.py` — `build_attachment_downloader(settings)` 추가(source_type=="atlassian"
  이면 authed httpx client 로 `HttpAttachmentDownloader`, 아니면 None). `build_chunking_worker_deps`
  실 branch 에 `attachment_downloader=build_attachment_downloader(resolved)` 배선.
- `tests/ingestion/test_bootstrap_attachment_downloader.py`(신규) — fixture→None / atlassian→
  HttpAttachmentDownloader(download_dir) 단위 회귀.

**범위 밖(후속)**: 실 atlassian 어댑터의 첨부 메타(download_url) 수집(vendored 경계) — 이게 돼야
라이브 end-to-end 동작. credential SOURCE 는 현 settings 패턴을 따르며, v2.5 `adminUserId`→
auth-server 조회로의 이전은 모든 Confluence client 공통 후속이다. 실 branch 전체 빌드는 E5/Qdrant
로딩이 필요해 샌드박스 비검증(헬퍼 단위로 wiring 핵심 커버).

**검증**: 변경 2파일 `ruff format`/`ruff check`(All passed) + `mypy app`(60 files, no issues) +
py_compile 통과. 전체 `pytest`·`./scripts/verify.sh` 는 Mac/3.11.
