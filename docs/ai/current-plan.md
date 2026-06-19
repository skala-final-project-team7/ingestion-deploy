# Current Plan — Data Ingestion Pipeline

이 문서는 현재 진행 중인 작업의 Plan을 기록한다. 구현 전에 작성하고, 작업 중 계획이 바뀌면 함께 수정한다.
하나의 feature가 끝나면 체크 처리하고, 모든 feature가 끝나면 새 세션에서 다음 Plan을 작성한다.

> **상태: 초기 스캐폴드 직후 (2026-05-26).** RAG 레포(`../rag`)에서 분리해 Data Ingestion Pipeline을
> 독립 저장소로 시작. 청킹·임베딩 자산은 복사 완료, 수집·동기화·큐 Worker는 신규 구현 예정.

> **★ ADR 0003 반영 (2026-05-26) — 아래 본문의 ACL/enum/soft_delete TBD는 대부분 해소됨.**
> ingestion↔rag 공유 계약 합의(`docs/adr/0003-ingestion-rag-shared-contracts.md`)로 다음이 확정/적용됐다.
> 본문의 개별 TBD 문구보다 ADR 0003이 우선한다.
> - **항목 1 ACL 모델 (api-spec v2.4/v2.5)**: page-level `allowed_groups`/`allowed_users` **채택**(Admin Key read restriction + 빈 권한 시 공개 sentinel `"*"`). `space:{key}` 합성(ADR 0002 prefix)은 Admin-Key-OFF 폴백. 더 이상 "결정 대기" 아님.
> - **항목 3 `IngestionStage.CRAWL`**: **적용됨**. enum 추가 + `crawler.run_full_crawl(jobs=...)` +
>   `pipeline` 에서 crawl·worker 가 jobs 공유 → crawl 단계 `ingestion_jobs` 기록 가능(더 이상 보류 아님).
> - **항목 4 soft_delete**: **능력 도입됨**(payload `is_deleted`, 검색 `must_not` 제외, store
>   `soft_delete_by_*`). Delta 삭제 트리거는 `/ml/ingest` delta 잡에 **배선됨**(확인 게이트
>   `data_sync_delta_delete_confirm`, 기본 OFF). Trash/Webhook 의 주기 실행 loop 만 운영 후속.
> - **합의 불필요**: `access_token`/`cloud_id` 전달(Auth/BFF 소관) — 두 레포 간 결정 대상 아님.
> - **남은 운영 wiring(인프라/미존재 컴포넌트 의존)**: featureI-7c(RabbitMQ consumer loop·실 crawl jobs
>   주입), soft_delete 삭제 트리거 실배선.

---

## 작업 개요

- **작업 목표**: Confluence 문서·첨부 수집 → 추출 → 청킹 → 임베딩 색인 + 동기화까지 동작하는 수집 파이프라인 MVP
- **담당 영역**: Data Ingestion Pipeline 전체 (`app/`, `tests/`) — 요구사항정의서 **FR-001~FR-005**
  - FR-001 데이터 수집 에이전트(Confluence Page+첨부 수집) / FR-002 첨부 텍스트 추출기 /
    FR-003 문서·파일 유형 분류 + Adaptive Chunker / FR-004 Dual Embedding + Multi-Pool 색인 /
    FR-005 데이터 동기화 에이전트(Delta Sync + 3중 삭제). (FR-006~ 질의/응답·피드백·대시보드는 RAG/BFF 영역)
- **브랜치 규칙**: feature별로 `feat/#<이슈번호>/<기능-이름>`
- **근거 문서**: 외부 **요구사항정의서 v0.2.1**(§2.0 Multi-Agent / §2.2 FR-001~005 / §3 데이터 요구사항),
  **아키텍처 다이어그램**(Data Ingestion Pipeline: Data Sync/Ingestion Agent + Chunking + Embedding,
  RabbitMQ, Confluence·GPT 소스, MySQL/MongoDB/Qdrant, Logging&Monitoring),
  `docs/architecture.md`, `docs/rag-pipeline-design.md`(§3·§5·§7), `docs/chunking-strategy.md`,
  `docs/atlassian-api.md`(DATA-01~03), `docs/db-schema.md`

## 선행 확인 / 의존성

- [x] **RAG 레포 자산 복사** — schemas / chunker / embedder / embedding·vector_store·indexer /
  adapters / storage / attachment_analyzer / sync (2026-05-26, import 경로 그대로 미러링)
- [ ] **`pip install -e ".[ingestion,embedding,dev]"`** 후 import·테스트 통과 확인 (Mac/3.11)
- [ ] **RabbitMQ / MongoDB / Qdrant 로컬 기동**(docker compose) — Worker 통합 테스트 전 필요
- [ ] **Confluence credential 전달 경로 (api-spec v2.6.2 §2-5)** — preferred: BFF/RabbitMQ payload 에
  credential 미포함, Worker 가 `adminUserId` 로 auth-server 내부 credential API 조회 → 응답
  `{accessToken, cloudId, siteUrl, expiresAt}` (siteUrl=출처 URL 정규화용 — 2026-06-11 추가,
  `RAG_ATLASSIAN_SITE_URL` 로 주입 가능). `accessToken`/`cloudId` 직접 전달은 legacy PoC 호환만
  (요구사항정의서 §2-2)
- [x] **수집 완료 이벤트 seam (api-spec v2.5.0)** — terminal(COMPLETED/FAILED) 직후 credential 없는
  RabbitMQ completion event 발행(`app/api/ingest_completion.py`). 실 RabbitMQ publisher/consumer·
  `adminUserId` credential lookup wiring 은 후속(featureI-7c).

## 현재 Change-set — CI Docker 빌드 디스크 고갈 완화 (2026-06-19)

- **작업 목표**: `build-push ingestion-worker` GitHub Actions job 에서 Docker buildx 중 러너 디스크가
  가득 차 `_diag/Worker_*.log` 기록까지 실패하는 문제를 완화한다.
- **수정 대상 파일**: `.github/workflows/build-and-deploy.yml`, `docs/ai/current-plan.md`
- **수정하지 않을 파일**: `Dockerfile.ingestion-*`, `app/**`, DB/RabbitMQ/Confluence 계약 문서
- **예상 영향 범위**: Harbor 이미지 빌드 job 의 사전/사후 디스크 정리와 디스크 사용량 진단 로그만 변경.
  애플리케이션 런타임과 이미지 태그/manifest 갱신 계약은 변경하지 않는다.
- **테스트 방법**: 워크플로 YAML 구조 확인, `git diff` 검토. 로컬에서 GitHub-hosted runner 디스크 상태는
  재현하지 않는다.
- **완료 기준**: 빌드 전에 대용량 호스팅 툴체인과 Docker 잔여물을 정리하고, 빌드 후에도 Docker/Buildx
  캐시를 정리하며, `df -h` 로그로 원인 파악이 가능하다.
- **문서 수정 필요 여부**: 현재 Plan 기록만 필요. 아키텍처/DB 스키마 변경 없음.

---

## Milestone A — 스캐폴드·기반 (현재)

### featureI-1: 저장소 스캐폴드  ✅ 완료 (2026-05-26)

- [x] 디렉토리·git·pyproject·scripts·.gitignore
- [x] 문서(CLAUDE.md / architecture / conventions / db-schema / workflow / prompt-templates)
- [x] 청킹·임베딩·schemas·adapters·storage 자산 복사 + import 정합(schemas/__init__ 트림)
- [x] 신규 컴포넌트 stub (crawler / extractor / workers)
- [ ] `./scripts/verify.sh` 통과 확인 (Mac/3.11 — 의존성 설치 후)

## Milestone B — 수집 (FR-001 / FR-002)

### featureI-2: Data Ingestion Agent — Confluence Full Crawl (FR-001)  ✅ featureI-6 으로 구현

> **구현 경로**: 본 feature는 신규 작성이 아니라 **featureI-6(외부 에이전트 vendoring 통합)**으로 구현한다.
> Data Ingestion Agent를 무수정 vendoring하고 `AtlassianSourceAdapter`/`crawler.run_full_crawl` 어댑터로
> 잇는다. 아래 흐름·완료 기준은 그대로 유효하다.

- **작업 목표**: Confluence 전체 문서를 초기 수집(Full Crawl)해 `raw_pages`/`raw_attachments`(MongoDB)에
  적재하고 Chunking Queue로 인계한다. `DocumentSourceAdapter` 계약을 따르는 `AtlassianSourceAdapter`를
  신규 구현하고, `crawler.run_full_crawl`이 이를 오케스트레이션한다.
- **브랜치**: `feat/#2/confluence-full-crawl` (skeleton 브랜치와 분리 — 이슈번호는 팀 규칙 따름)
- **근거**: 요구사항정의서 FR-001, `docs/atlassian-api.md`(DATA-01 Full Crawl / DATA-03 Space 목록 /
  PageObject 매핑), `docs/architecture.md`, `docs/db-schema.md`.

#### 수집 흐름 (FR-001 1~8단계)

1. **Space 목록** — `DATA-03 GET /space`(사용자 접근 가능 Space만 자동 반환). `/ml/ingest`는 spaceKey를
   받지 않으며 **접근 가능한 전체 스페이스를 cross-space 로 iterate** 수집한다(api-spec v2.4 — 단일 spaceKey 입력 제거).
2. **페이지 Full Crawl** — Space별 `DATA-01 GET /content?type=page&spaceKey=...&expand=space,version,body.storage,metadata.labels,ancestors`
   를 `limit≤100` 페이지네이션 반복(`atlassian-python-api`의 `get_all_pages_from_space_as_generator`).
   ※ 요구사항의 homepage→descendants 트리 순회는 DATA-01 페이지네이션으로 대체(스페이스 전체 페이지 동등 수집).
3. **PageObject 매핑** — `docs/atlassian-api.md` 매핑표대로 id/title/body.storage→body_html/version/
   last_modified/space_key/labels/ancestors/webui_link/attachments[].
4. **ACL 적재** — page-level read restriction → `allowed_groups`/`allowed_users`(Admin Key on), 빈 권한은 공개 sentinel `"*"`. Admin-Key-OFF 폴백은 `_synthesize_acl`(`space:{space_key}`). 결정 완료 — ADR 0003 항목 1 / db-schema §1.4.
5. **첨부 다운로드** — `attachments[]`의 다운로드 URL로 바이너리 수집(PDF/Word/Excel). 실패는 페이지
   전체 실패로 전파하지 않고 격리(graceful degrade) 후 기록.
6. **MongoDB 적재** — `raw_pages`(페이지 원본 JSON + PageObject) / `raw_attachments`(메타 + 바이너리 핸들).
7. **Chunking Queue 발행** — `content.chunking` 라우팅 키로 후속 메시지 발행(pika). 첨부는 FR-002로 라우팅.
8. **실패 처리** — Rate Limit(429) 지수 백오프(tenacity), 실패 페이지/첨부 재시도 또는 DLQ 보류.
   진행/결과(성공·실패·스킵·소요시간)는 `ingestion_jobs`(MongoDB)에 기록.

#### 수정 대상 파일

- `app/adapters/atlassian.py` (신규) — `AtlassianSourceAdapter(DocumentSourceAdapter)`:
  `fetch_pages(since=None)`(None=Full Crawl, since=Delta는 FR-005에서 활용) / `list_active_ids`
  (Reconciliation용) / `watch_changes`(미지원 → 빈 스트림). `atlassian-python-api`로 DATA-01/03 호출,
  `access_token`+`cloudid` 주입, Rate Limit 백오프.
- `app/adapters/factory.py` — `source.type="atlassian"` 분기 추가(기존 json_fixture 분기 유지).
- `app/ingestion/crawler.py` (stub→구현) — `run_full_crawl(CrawlRequest)`: 어댑터 fetch_pages 순회 →
  raw 적재 → 첨부 다운로드 → 큐 발행 → `CrawlResult` 집계.
- `app/storage/raw_store.py` (신규) — `raw_pages`/`raw_attachments` 적재 헬퍼(기존 `mongo_cache` 패턴 재사용).
- `app/ingestion/workers/ingestion_worker.py` (신규) — Ingestion 큐 소비 + Chunking 큐 발행(pika).
- `app/storage/jobs.py` (기존 확장) — `ingestion_jobs` 기록.
- `app/config.py` — Confluence base URL·cloudid 경로·`source.type` 설정.
- `docs/db-schema.md` — `raw_pages`/`raw_attachments`(extracted_text 포함) 컬렉션 스키마 추가 — 별도 attachment_texts 미사용(§2.7).
- `tests/adapters/test_atlassian.py`, `tests/ingestion/test_crawler.py` (신규).

#### 수정하지 않을 파일

- `app/ingestion/chunker/`·`embedder/`·`embedding.py`·`vector_store.py`·`indexer.py` (FR-003/004 — featureI-4)
- `app/ingestion/sync.py` (FR-005 — featureI-5), `app/ingestion/extractor/` (FR-002 — featureI-3)
- `app/schemas/*` (PageObject/Attachment 기존 활용 — 변경 불필요)

#### 선행 의존성 / 결정 필요

- [x] ★ **ACL 모델 결정 — 완료 (api-spec v2.4/v2.5, ADR 0003 항목 1)**: page-level `allowed_groups`/
  `allowed_users` **채택**(Admin Key read restriction + 빈 권한 시 공개 sentinel `"*"`). `space:{key}`
  합성(`_synthesize_acl`)은 Admin-Key-OFF 폴백, rag `build_acl_filter` 가 검색 seam. **RAG 레포 공유 계약 — `docs/adr/0003-ingestion-rag-shared-contracts.md` 참조.**
- [ ] **credential 전달 경로 (api-spec v2.6.2 §2-5)** — preferred: Worker 가 `adminUserId` 로 auth-server
  내부 credential API 조회(BFF/RabbitMQ payload 에 credential 미포함) → `{accessToken, cloudId,
  siteUrl, expiresAt}`. `accessToken`/`cloudId` 직접 전달은 legacy PoC 호환만(요구사항 §2-2).
  webui_link absolute 정규화는 siteUrl(=`RAG_ATLASSIAN_SITE_URL`)로 **반영 완료**(2026-06-11).
- [ ] **RabbitMQ / MongoDB 로컬 기동**(docker compose) — 통합 테스트 전.

#### 테스트 방법 (외부 의존성 mock/fake)

- mock HTTP로 DATA-03 Space 목록 → DATA-01 페이지 페이지네이션 순회, `_links.next`/`start` 반복.
- PageObject 매핑 정합(매핑표 전 필드), ACL 합성, 첨부 메타 매핑.
- `raw_pages`/`raw_attachments` 적재(fake Mongo), Chunking Queue 메시지 형식·라우팅 키(fake pika).
- Rate Limit 429 → 지수 백오프 재시도, 첨부 다운로드 실패 격리, 실패 페이지 DLQ.
- `crawler.run_full_crawl` end-to-end(어댑터+storage+queue 전부 fake) → `CrawlResult` 집계 검증.

#### 위험 요소

- ACL 모델은 확정(page-level `allowed_groups`/`allowed_users` + allow_authenticated `"*"`, ADR 0003) — 단 Admin Key 실연동 전까지는 `space:{key}` 폴백 합성으로 동작하므로, 운영 전환(`RAG_ATLASSIAN_USE_ADMIN_KEY=true`) 시 page-level 적재 검증 필요.
- 대용량 Full Crawl 시간/메모리 — 제너레이터 스트리밍 + 배치 적재로 완화.
- API Rate Limit / 첨부 다운로드 불안정 — 백오프·격리·DLQ로 대응.

#### 완료 기준

- `AtlassianSourceAdapter`가 `DocumentSourceAdapter` 계약(3개 메서드) 충족 + 단위 테스트 통과.
- mock Full Crawl end-to-end(Space→페이지→raw 적재→Chunking Queue 발행) 통과.
- `./scripts/verify.sh`(format→lint→test) 통과.
- `docs/db-schema.md`에 `raw_pages`/`raw_attachments` 스키마 추가, `docs/ai/working-log.md` 기록.
- ACL/토큰 전달의 미해결 결정은 문서에 명시(추측 구현 금지).

### featureI-3: 첨부 텍스트 추출기 (FR-002)  ✅ 추출기 코어 완료 / featureI-3b 첨부 체인 배선 완료

- **목표**: 첨부 바이너리(PDF/Word/Excel/CSV)를 텍스트로 추출하는 **결정론 Pipeline** 구현
  (이미지·도형 제외). Excel/CSV는 시트→자연어 직렬화. self-contained(공급원 무관, bytes→text).
- **브랜치**: `feat/#9/attachment-extractor`.
- **스코프 결정**: vendored 에이전트 MVP가 첨부를 수집하지 않아 `raw_attachments` 입력이 없으므로,
  이번엔 **추출기 코어 + 단위 테스트**만 구현한다. 첨부 **수집기**(Confluence Attachment API 다운로드
  → `raw_attachments` 적재)와 **Attachment Queue 배선**은 후속(featureI-3b — 텍스트는 `raw_attachments.extracted_text` 에 보존, 별도 attachment_texts 컬렉션 미사용).
  수집은 에이전트/어댑터 확장 또는 별도 Confluence 클라이언트가 선행돼야 한다(TBD).
- **수정/신규**:
  - `app/ingestion/extractor/base.py`(stub→구현) — `extract_attachment_text` 유형 디스패치 +
    graceful degrade(실패 시 `ok=False`/`reason`, 쿼리 전체 실패로 전파 금지).
  - `app/ingestion/extractor/pdf.py`(신규) — PyMuPDF(fitz) 1차 → pdfplumber 폴백. `RAW_TEXT`.
  - `app/ingestion/extractor/docx.py`(신규) — python-docx 본문 문단+표. `RAW_TEXT`.
  - `app/ingestion/extractor/spreadsheet.py`(신규) — openpyxl(xlsx)/csv → 시트 자연어 직렬화. `SHEET_SERIALIZED`.
  - 라이브러리는 함수 내 **지연 import**(app import 가 ingestion extras 미설치에서도 동작).
- **테스트(외부 파일 in-test 생성)**: 유형별 추출(python-docx/openpyxl/fitz로 최소 파일 생성 후 추출),
  시트 직렬화 형식, 손상 바이너리 graceful degrade(`ok=False`), 빈/텍스트 없음 처리.
- **완료 기준**: 4유형 추출 + 실패 격리 단위 테스트 통과, `verify.sh` 통과. 수집기·큐 배선은 TBD 명시.

#### featureI-3b: 첨부 청킹 체인 배선  ✅ 구현 완료 (2026-05-26)

> **설계 결정(사용자 승인)**: 첨부 청킹은 **rag 레포 ingestion 그래프와 동일하게 파일 기반
> `chunk_attachment`** 로 처리한다. 별도 `attachment_texts` 컬렉션은 청킹 경로에 두지 않고,
> `extracted_text` 는 `raw_attachments` 에 함께 보존한다(analyze_attachment 유효성 게이트 입력).
> 작업 범위는 **Fake 로 검증 가능한 전부**이며, 실 Confluence 첨부 다운로드 어댑터와 pika
> consumer 실행 loop 는 인프라 의존 후속(featureI-3c/featureI-7c)으로 남긴다.

- **구현/수정 파일**:
  - `app/storage/raw_store.py` — `get_attachment(attachment_id)` 읽기 메서드(ABC/Fake/Mongo).
  - `app/ingestion/workers/chunking_worker.py` — `process_chunking_message` 가 `source_type` 으로
    본문/첨부 분기. `_process_attachment_message`(raw_attachments 로드 → 부모 ACL 게이트 →
    `analyze_attachment` → `chunk_attachment_fn` → `index_chunks(attachment_download_urls)`).
    `AttachmentNotFoundError`, `ChunkAttachmentFn`, `ChunkingWorkerDeps.chunk_attachment_fn`(주입),
    `ChunkingMessageResult.attachment_id`, `_record(stage, attachment_id)` 확장.
  - `app/ingestion/crawler.py` — `build_attachment_chunking_message` + `run_full_crawl` 이
    `page.attachments` 적재(`save_attachment`) + 첨부 `content.chunking` 발행. `CrawlResult.
    failed_attachment_ids`(첨부 단위 격리), 첨부 CRAWL 잡 기록.
  - `app/ingestion/pipeline.py` — `build_poc_components`/`run_poc_ingestion` 에
    `chunk_attachment_fn` 주입(파일 시스템 없이 첨부 전 체인 e2e).
  - `docs/db-schema.md` §2.7 `raw_attachments` 필드·적재 흐름 확정.
- **테스트**: `tests/ingestion/test_chunking_worker.py`(첨부 청킹/ACL 상속/미지원·저품질/암호화·
  기타 ValueError 격리/멱등성/누락 첨부·부모/혼합 디스패치), `test_crawler.py`(첨부 적재·발행·
  잡 기록·실패 격리), `test_pipeline_e2e.py`(첨부 전 체인 + 멱등성), `test_raw_store.py`(get_attachment).
  외부 파일 의존성은 `chunk_attachment_fn` fake 주입으로 회피. 검증 ruff/format/py_compile 통과
  (전체 pytest·`verify.sh` 는 Mac/3.11 — 샌드박스 StrEnum 제약).
- **TBD(후속)**: 실 Confluence 첨부 **다운로드 어댑터**(바이너리 수집 → `local_path`/
  `extracted_text` 채움, 인프라 의존), pika consumer/publisher 실행 loop(featureI-7c 와 공통).

## Milestone C — 청킹·임베딩 Worker (FR-003 문서·파일 유형 분류 + Adaptive Chunker / FR-004 Dual Embedding 색인)

### featureI-4: 문서·파일 유형 분류 + Chunking / Embedding Worker (FR-003 / FR-004)  ✅ 구현 완료 (단일 Worker, 2026-05-26)

> **상태: 구현 완료 (단일 Worker 토폴로지).** `content.chunking` 소비 → `raw_pages.get_page` →
> `chunk_page`(doc_type 라벨 폴백) → `index_chunks`(Dual Embedding + Qdrant Multi-Pool upsert +
> `embedding_cache` 멱등성) → `ingestion_jobs`(stage UPSERT) 배선. 검증 ruff/mypy/Fake end-to-end
> 통과(전체 pytest·verify.sh 는 Mac). **후속**: featureI-4b(GPT-4o-mini 문서 분석기[Agent] +
> MySQL `space_doc_type_cache`), 첨부 청크 경로(FR-002 이후), 실 어댑터 부트스트랩 + pika consumer
> 배포 wiring. 신규: `workers/consumer.py`·`workers/chunking_worker.py`, `raw_store.get_page`,
> `tests/ingestion/test_chunking_worker.py`. 상세는 `docs/ai/working-log.md` 참조. (아래는 원 Plan.)

- **작업 목표**: featureI-6로 연결된 앞 절반(crawl → `raw_pages` 적재 → `content.chunking` 발행)을
  이어받아 **`content.chunking` 메시지를 소비 → Adaptive Chunker → Dual Embedding → Qdrant upsert**
  까지 배선해 수집 파이프라인을 end-to-end로 동작시킨다. chunker/embedder/embedding/vector_store/
  indexer 는 이미 복사돼 있고, **소비 Worker만 없다.**
- **브랜치**: `feat/#7/chunking-embedding-worker` (feat/#6 머지 후 분기 권장 — 스택 방지).
- **근거**: 요구사항정의서 FR-003/FR-004, `docs/architecture.md`, `docs/chunking-strategy.md`, `docs/db-schema.md` §1.

#### 핵심 설계 결정 (구현 전 확정 필요)

- ★ **Worker 토폴로지**: 복사된 `indexer.index_chunks` 가 **임베딩+upsert+cache 를 한 흐름으로 결합**한다.
  - **(A) 단일 Chunking+Embedding Worker (PoC 권장)**: `content.chunking` 소비 → `raw_pages` 로드 →
    `chunk_page` → `index_chunks`(embed+upsert). `content.embedding` 큐는 상수로 예약만. 부품이 적고
    Chunk 직렬화 불필요. 아키텍처 다이어그램의 4큐 중 Embedding 큐를 생략하는 절충.
  - **(B) 2-Worker (다이어그램 정합)**: Chunking Worker(→`content.embedding` 에 Chunk payload 발행) +
    Embedding Worker(Chunk 복원 → `index_chunks`). 다이어그램에 충실하나 Chunk 직렬화/역직렬화 필요.
  - → PoC는 (A)로 진행하고 (B)는 운영 스케일링 시 분리하도록 문서화 제안(확정은 구현 착수 시).
- **문서 분석기 [Agent]**: 우선 **결정론 폴백**(`chunker.body.infer_doc_type` 휴리스틱 / 'operation')로
  배선해 end-to-end 를 먼저 닫고, **GPT-4o-mini + Function Calling + MySQL `space_doc_type_cache`** 는
  후속 sub-feature(featureI-4b)로 분리. (rag 미구현 → 본 레포 신설.)

#### 수정/신규 대상 파일

- `app/ingestion/workers/chunking_worker.py` (신규) — `content.chunking` 소비 → raw_pages 로드 →
  doc_type 판별(폴백) → `chunk_page` → `index_chunks` → `ingestion_jobs` 기록(stage `chunk`/`embed`/
  `upsert` — **enum 이미 존재**, crawl 과 달리 기록 가능). 큐 소비 추상화(Consumer ABC + Fake + Pika).
- `app/ingestion/workers/__init__.py` — Consumer/Worker export.
- `app/storage/raw_store.py` (확장) — `get_page(page_id) -> PageObject | None` **읽기** 메서드 추가
  (현재 save 만 존재). Fake/Mongo 양쪽.
- `app/ingestion/document_analyzer.py` (신규, featureI-4b) — 문서 분석기 [Agent]. featureI-4 본편은
  폴백만, LLM 판별은 후속.
- 운영 의존성 빌더(실 E5/BM25 임베더 + Qdrant from_settings)는 `app/config.py` `use_real_adapters`
  토글 패턴 재사용. 테스트는 Fake 임베더 + in-memory Qdrant/Fake cache 주입.
- 첨부 경로(`chunk_attachment`)는 첨부 입력이 생기는 FR-002(featureI-3) 이후 연결 — 본편은 본문 청크만.

#### 테스트 (외부 의존성 mock/fake)

- end-to-end: `content.chunking` 메시지 → Worker → fake raw_store(get_page) → `chunk_page` →
  Fake 임베더 + Fake Qdrant + Fake cache → upsert 카운트/`chunk_id` 검증.
- 멱등성: 동일 `(chunk_id, version_number)` 재실행 시 `index_chunks` skip(캐시 히트) 검증.
- doc_type 폴백 분기, `ingestion_jobs` 단계별(chunk/embed/upsert) 기록, 메시지 형식 round-trip.

#### 완료 기준

- crawl → publish → **chunking_worker 소비 → Qdrant upsert** 가 fake end-to-end 로 통과(멱등성 포함).
- `./scripts/verify.sh` 통과, `docs/architecture.md`(Worker 상태)·`docs/ai/working-log.md` 갱신.
- LLM 문서 분석기·첨부 경로·`content.embedding` 분리는 후속(featureI-4b/featureI-3)으로 문서화.

#### 선행/TBD

- ACL·access_token/cloud_id 전달 경로는 featureI-6와 동일 TBD(검색 정확도/실연동 — 병렬 협의).
- 실 임베딩 모델(E5 ~2.4GB)·Qdrant 서버는 통합 테스트에만 필요. 단위/CI 는 Fake 로 대체.

### featureI-4b: 문서 분석기 [Agent] — GPT-4o-mini doc_type 판별 + MySQL 캐싱 (FR-003)  📋 진행 중

- **작업 목표**: featureI-4 의 라벨 휴리스틱 폴백을 대체해, **스페이스 단위 1회** 본문 doc_type 을
  6유형(incident/operation/faq/meeting/adr/troubleshoot)으로 LLM 판별하고 MySQL
  `space_doc_type_cache`(db-schema §3.1)에 캐싱한다. 이후 같은 스페이스의 모든 페이지가 캐시를 재사용.
- **브랜치**: `feat/#8/document-analyzer` (feat/#7 머지 후 분기 권장).
- **분류 [Agent] 규칙(app/CLAUDE.md §5)**: GPT-4o-mini, Function Calling 으로 스키마 강제, 타임아웃 +
  Fallback. 신뢰도 < 0.6 또는 LLM 실패 시 `DocType.OPERATION` 폴백(DocType 에 'general' 없음 — db-schema
  confidence 주석과 정합. CLAUDE.md 의 'general' 표기와의 차이는 문서화).
- **수정/신규 대상**:
  - `app/storage/space_doc_type_cache.py`(신규) — `SpaceDocTypeCache` ABC + `FakeSpaceDocTypeCache` +
    `MySQLSpaceDocTypeCache`(sqlalchemy) + `SpaceDocTypeEntry`. db-schema §3.1 정합.
  - `app/ingestion/document_analyzer.py`(신규 [Agent]) — `DocTypeClassifier` ABC + `FakeDocTypeClassifier`
    + `OpenAIDocTypeClassifier`(GPT-4o-mini, Function Calling, 타임아웃) + `DocumentAnalyzer.resolve_doc_type`
    (캐시 우선 → 미스 시 분류 → 캐싱 → 폴백). LLM 호출은 어댑터 경계에 격리(테스트는 Fake).
  - `app/ingestion/workers/chunking_worker.py`(확장) — `ChunkingWorkerDeps.doc_type_resolver`(optional)
    추가. 주입 시 `chunk_page(page, resolver.resolve_doc_type(page))`, 미주입 시 기존 라벨 폴백(무변).
  - `app/storage/__init__.py` export, (`app/config.py` 의 `llm_aux_model`/`openai_api_key`/`mysql_uri` 재사용).
- **테스트(외부 mock)**: 캐시 미스→분류→캐싱, 캐시 히트 재사용, 저신뢰/예외→OPERATION 폴백,
  Worker 가 resolver doc_type 으로 청킹. OpenAI·MySQL 은 Fake 로 대체.
- **완료 기준**: Worker 가 resolver 주입 시 LLM doc_type 으로 청킹 + 스페이스 1회 판별 캐싱. `verify.sh` 통과.
- **TBD**: 다중 샘플 스페이스 분석(PoC 는 첫 페이지 1샘플), 실 OpenAI/MySQL 부트스트랩 wiring.

### featureI-7: 파이프라인 조립(composition) + in-process end-to-end PoC  📋 진행 중

- **작업 목표**: featureI-6(crawl→raw_pages→`content.chunking`)·featureI-4(chunking_worker→Qdrant)로
  나뉜 두 절반을 **하나의 시스템으로 in-process 조립**해 end-to-end 동작을 검증한다. 운영은 큐로
  분리된 독립 Worker 지만, PoC/로컬/통합 테스트는 in-process 합성으로 전체 흐름을 확인한다.
- **브랜치**: `feat/#10/pipeline-composition`.
- **수정/신규**:
  - `app/storage/qdrant_fake.py`(신규) — `FakeQdrantPoolStore`(in-memory). `upsert_chunks_batch`/
    `scroll_page_ids`/`scroll_attachment_ids`/`delete_by_page_id`/`delete_by_attachment_id`/
    `delete_by_chunk_id` 구현(indexer·reconcile 인터페이스 호환). **공유 `qdrant_client.py` 무수정**
    (additive — 새 모듈). rag 설계의 "Fake everything" PoC 모드 enabler.
  - `app/ingestion/pipeline.py`(신규) — `run_ingestion_pipeline(request, *, source, raw_store,
    publisher, chunking_deps)`: crawl 실행 → 발행된 `content.chunking` 메시지를 worker 로 drain →
    `PipelineResult`. `build_poc_components`(all-fakes, raw_store 공유) + `run_poc_ingestion` 편의 함수.
  - `app/storage/__init__.py` export.
- **테스트**: fake source(또는 fixture) → crawl → raw_pages 적재 → publish → chunking_worker →
  `FakeQdrantPoolStore` upsert 를 **전 체인** 검증. 재실행 멱등성(캐시 skip), ACL 누락 페이지 전파 차단.
- **완료 기준**: 전 체인 end-to-end 테스트 통과 + `verify.sh`. 운영 합성(real adapters + pika consumer
  loop)은 featureI-7b TBD 로 명시.
- **featureI-7b (구현 완료)**: `app/ingestion/bootstrap.py` — Settings 기반 의존성 조립
  (`build_raw_page_store`/`build_document_analyzer`/`build_chunking_worker_deps`, PoC Fake vs 실 어댑터
  지연 import). PoC 모드 단위 테스트 완료.
- **TBD(후속 featureI-7c)**: pika consumer/publisher 실행 loop + CLI 엔트리포인트(RabbitMQ 연결 —
  인프라 의존, 통합 환경에서 검증). `Settings` 에 rabbitmq_url 추가 필요.

## Milestone D — 데이터 동기화 에이전트 (FR-005)

### featureI-5: 데이터 동기화 에이전트 — Delta Sync + 3중 삭제 동기화 (FR-005)  ✅ Delta+Reconcile=featureI-6 / Trash·Webhook 트리거=featureI-5b

> **구현 경로**: 본 feature도 **featureI-6(외부 에이전트 vendoring 통합)**으로 구현한다. Data Sync Agent를
> 무수정 vendoring하고 `app/ingestion/sync.py`(기존 `reconcile_deletions` 보존) + 어댑터로 잇는다.

- **목표**: 주기(기본 1시간) Delta Sync — Confluence API로 Space/Page/첨부 메타 수집, MongoDB 원본
  (`version`/`updatedAt`) 비교로 변경·삭제 페이지만 식별 → 변경분만 FR-001~FR-004 동일 파이프라인 재투입
  (본문 재수집 → chunk 재생성 → Vector DB upsert).
- **3중 삭제 동기화**: (1) Confluence **Trash API**로 삭제(Trashed) 페이지·첨부 조회 → Qdrant payload
  `soft_delete` (소프트 삭제), (2) **Webhook**(실시간 삭제 이벤트), (3) 주 1회 **Reconciliation**(고스트 데이터 제거).
  > **ADR 0003 항목 4 반영**: soft-delete **능력은 도입됨** — payload `is_deleted`, rag 검색 `must_not`
  > 제외, `store.soft_delete_by_page_id`/`soft_delete_by_attachment_id`. hard delete 도 보존(soft/hard
  > 호출 측 선택). **남은 것은 트리거 실배선**(Trash/Webhook/`deleted_candidate` → `soft_delete_by_*`
  > 호출)으로, store 를 소유한 Sync Worker 운영 wiring 후속이다. 현재 `run_delta_sync` 는
  > `deleted_candidate` 를 *확인 대기* 목록으로 surface 만 한다(자동 삭제 아님).
- 수정 대상: `app/ingestion/sync.py`(복사된 `reconcile_deletions` 확장), `app/ingestion/workers/`
- 재사용: `sync.py`의 `reconcile_deletions`(복사 완료, Reconciliation 중심)
- 테스트: 변경/삭제 식별, 고스트 삭제, Reconciliation 멱등성, Delta 재투입 흐름

---

## Milestone E — 외부 에이전트 2종 vendoring 통합 (featureI-6)  ✅ 구현 완료 (2026-05-26)

> **상태: 구현 완료 (2026-05-26).** Data Ingestion Agent(FR-001)·Data Sync Agent(FR-005)
> 두 패키지를 수신해 vendoring → 어댑터 → 큐 배선 → 테스트까지 완료. 검증은 ruff(통과) +
> py_compile 까지 샌드박스(Python 3.10)에서 수행. **전체 pytest·`./scripts/verify.sh`·push 는
> Mac(3.11)에서 수행 필요**(샌드박스는 vendored 의 StrEnum 미지원 + 의존성 미설치).
>
> **결정 결과**: (1) vendoring 레이아웃 = 패키지를 저장소 루트로(rag 미러), 에이전트
> `scripts/`+`tests/` 는 `tests/<agent>/` 에 무수정 + `__init__.py` 마커. (2) 어댑터 구동 =
> 에이전트 상단 workflow(`run_full_crawl_workflow`/`run_data_sync_workflow`) in-process
> 블랙박스 호출 + 산출물 메모리 변환(로컬 파일 출력은 임시 디렉토리로 우회).
>
> **수신 에이전트 실측 vs 계획 차이**: 에이전트 MVP 는 ① ACL ② labels/ancestors
> ③ 첨부(not_supported_in_mvp) 를 산출하지 않고, ④ 자체 `ProcessedDocument`(중첩
> space/page/body) 스키마를 쓴다. → 어댑터가 `PageObject` 로 변환하고 ACL 은 space_key 합성,
> ②③ 는 빈 값으로 둔다(모두 문서화된 TBD). ⑤ `IngestionStage` enum 의 crawl 단계 보류는
> **해소됨** — ADR 0003 항목 3으로 `CRAWL` 값 추가 + `pipeline` 에서 crawl·worker 가 jobs 공유해
> crawl 단계 `ingestion_jobs` 기록이 가능하다(`CrawlResult`/`DeltaSyncResult` 는 집계 리포트로 병행
> 유지). ⑥ snapshot 영속화는 에이전트 로컬 파일 기반(Mongo 영속화는 후속).
>
> 본 featureI-6은 featureI-2(Full Crawl)·featureI-5(Delta Sync + 3중 삭제)를 **"신규 작성"이
> 아니라 "외부 에이전트 vendoring + 얇은 어댑터"** 방식으로 구현한 상위 작업이다.
>
> **구현/수정 파일**: `data_ingestion_agent/`·`data_sync_agent/`(vendored), `tests/<agent>/`,
> `pyproject.toml`(include/exclude/agents extra), `app/adapters/atlassian.py`(신규),
> `app/adapters/factory.py`·`app/adapters/__init__.py`(atlassian 분기), `app/config.py`
> (atlassian placeholder), `app/storage/raw_store.py`(신규)·`app/storage/__init__.py`,
> `app/ingestion/workers/publisher.py`(신규), `app/ingestion/crawler.py`(구현),
> `app/ingestion/sync.py`(`run_delta_sync` 추가, `reconcile_deletions` 무수정),
> `tests/adapters/test_atlassian.py`·`tests/ingestion/test_crawler.py`·`test_sync.py`(신규),
> `tests/test_scaffold.py`(crawler stub 테스트 갱신), `docs/db-schema.md`·`docs/architecture.md`.

### 작업 목표

- 외부에서 받은 두 에이전트 패키지를 **무수정으로 저장소 루트에 vendoring**하고, ingestion 파이프라인은
  vendored 코드를 직접 호출하지 않고 `app/ingestion/`의 **얇은 어댑터**를 통해서만 연결한다.
- vendored 코드와 ingestion 계약(`PageObject`, `DocumentSourceAdapter`, 큐 메시지 형식)이 어긋나면
  **어댑터에서 변환**한다. vendored 원본은 절대 수정하지 않는다.
- 통합 완료 시 featureI-2(Full Crawl)·featureI-5(Delta Sync + 3중 삭제)의 완료 기준을 충족한다.

### 브랜치

- `feat/#6/vendor-ingestion-sync-agents` (이슈번호는 팀 규칙 — skeleton 브랜치와 분리)
- 1 change-set = 1 commit 지향. 규모가 크면 (1) vendoring+pyproject, (2) Ingestion Agent 어댑터,
  (3) Sync Agent 어댑터, (4) 큐 배선, (5) 테스트로 커밋을 분할한다.

### 1) Vendoring 레이아웃 (rag 미러링 — 반드시 준수)

```
ingestion/
├── data_ingestion_agent/      # ← 수신 패키지 무수정 vendoring (FR-001)
├── data_sync_agent/           # ← 수신 패키지 무수정 vendoring (FR-005)
├── app/                       # ingestion 본체 (어댑터가 vendored 호출)
└── tests/
    ├── data_ingestion_agent/  # 받은 테스트 무수정 배치 (+ __init__.py 만 허용)
    └── data_sync_agent/
```

- 받은 패키지 디렉토리명·내부 import 경로는 **그대로 보존**한다(원본 무수정). 실제 디렉토리명은
  수신 패키지에 맞춘다(`data_ingestion_agent`/`data_sync_agent`는 잠정명).
- 받은 테스트는 `tests/<agent>/`에 무수정 배치하되, pytest 패키지 인식용 `__init__.py`만 추가 허용한다.
- vendored 패키지가 자체 의존성을 요구하면 `pyproject.toml`에 **추가만** 하고 기존 의존성은 건드리지 않는다.

### 2) `pyproject.toml` 편집 (vendored 무수정 보존 장치)

```toml
[tool.setuptools.packages.find]
include = ["app*", "data_ingestion_agent*", "data_sync_agent*"]   # ← vendored 추가

[tool.ruff]
line-length = 100
target-version = "py311"
extend-exclude = ["data_ingestion_agent", "data_sync_agent"]      # ← lint 제외(원본 무수정)

[tool.mypy]
python_version = "3.11"
ignore_missing_imports = true
exclude = ["data_ingestion_agent/", "data_sync_agent/"]           # ← 타입체크 제외
```

- ruff/mypy 제외는 vendored 원본을 우리 컨벤션에 맞추려 수정하지 않기 위한 것이다(rag 패턴 동일).
- 어댑터(`app/ingestion/*`)는 **제외 대상이 아니다** — 우리 코드이므로 format/lint/type 전부 적용한다.

### 3) 어댑터 seam 설계 (vendored ↔ ingestion 계약)

#### (A) Data Ingestion Agent → Full Crawl (featureI-2 연계)

- **`app/adapters/atlassian.py` (신규)** — `AtlassianSourceAdapter(DocumentSourceAdapter)`.
  vendored Data Ingestion Agent를 in-process로 호출하고, 그 산출물을 표준 `PageObject` 스트림으로 변환한다.
  - `fetch_pages(since=None)`: `since=None`이면 vendored Full Crawl, `since`가 있으면 Delta 입력으로 위임.
  - `list_active_ids() -> ActiveIds`: Reconciliation용 살아있는 page_id/attachment_id 집합.
  - `watch_changes() -> Iterator[ChangeEvent]`: 미지원이면 빈 스트림.
  - vendored 출력 필드 ↔ `PageObject`(page_id/space_key/title/body_html/version_number/last_modified/
    labels/ancestors/webui_link/attachments) **매핑은 어댑터 내부에서** 수행(`docs/atlassian-api.md` 매핑표 기준).
  - **ACL**: vendored가 ACL을 제공하지 않으면 PoC `_synthesize_acl`(`["space:{space_key}"]`) 패턴으로 합성.
    `PageObject.is_acl_missing`이면 `INVALID_ACL`로 색인 제외(스키마 단 거부 금지).
  - **access_token/cloud_id**: `CrawlRequest`(crawler.py)에 이미 placeholder 필드 존재. 어댑터 생성자로
    주입하되 **로그·메시지·테스트 픽스처에 남기지 않는다**(app/CLAUDE.md §3).
- **`app/adapters/factory.py` (확장)** — `source_type=="atlassian"` 분기의 `NotImplementedError`를 제거하고
  `AtlassianSourceAdapter` 생성으로 교체(기존 `json_fixture` 분기 유지).
- **`app/ingestion/crawler.py` (stub→구현)** — `run_full_crawl(CrawlRequest)`가 어댑터를 오케스트레이션:
  `fetch_pages()` 순회 → `raw_pages`/`raw_attachments` 적재 → 첨부 다운로드(graceful degrade) →
  Chunking Queue(`content.chunking`) 발행 → `CrawlResult` 집계. 진행/결과는 `ingestion_jobs`에 기록.
- **`app/storage/raw_store.py` (신규)** — `raw_pages`/`raw_attachments` 적재 헬퍼(`mongo_cache`/`jobs.py`
  의 ABC + Fake + Mongo 3계층 패턴 재사용). featureI-2 plan 정합.

#### (B) Data Sync Agent → Delta Sync + 3중 삭제 (featureI-5 연계)

- **`app/ingestion/sync.py` (복사본 확장)** — 현재 `reconcile_deletions`(Reconciliation)만 존재.
  vendored Data Sync Agent를 어댑터로 연결해 Delta Sync(변경·삭제 페이지 식별 → 변경분만 FR-001~004
  재투입)와 3중 삭제(Trash API / Webhook / 주1회 Reconciliation)를 잇는다. 기존 `reconcile_deletions`
  시그니처·동작은 보존(비파괴 확장).
- vendored Sync 로직이 `DocumentSourceAdapter.fetch_pages(since=...)`/`list_active_ids()`/`watch_changes()`
  계약과 어긋나면 어댑터에서 변환한다. 삭제는 Qdrant payload `soft_delete`(소프트 삭제) →
  `store.delete_by_page_id`/`delete_by_attachment_id` cascade로 잇는다.
- **`app/ingestion/workers/sync_worker.py` (신규, featureI-5)** — 주기 트리거/Webhook 수신을 sync 어댑터에 배선.

#### (C) 공통 — 큐 배선

- 큐/라우팅 키 상수는 `app/ingestion/workers/__init__.py`의 `QUEUE_INGESTION="ingestion"` /
  `QUEUE_ATTACHMENT="content.extract.attachment"` / `QUEUE_CHUNKING="content.chunking"` /
  `QUEUE_EMBEDDING="content.embedding"`를 **그대로 사용**(신규 키 추가 금지, 필요 시 plan에서 합의).
- 발행 메시지 스키마(page_id/attachment_id/stage/라우팅 키)는 featureI-2/I-4 형식과 정합. pika 발행은
  fake로 테스트.

### 4) 수정하지 않을 파일

- `data_ingestion_agent/`·`data_sync_agent/` vendored 원본 전체(어댑터에서만 변환).
- `app/ingestion/chunker/`·`embedder/`·`embedding.py`·`vector_store.py`·`indexer.py`(FR-003/004 — featureI-4).
- `app/ingestion/extractor/`(FR-002 — featureI-3).
- `app/schemas/*`(PageObject/Attachment 계약 — 변경 불필요. 변경 필요 판단 시 RAG 분기 영향 먼저 설명).

### 5) 테스트 (외부 의존성 mock/fake)

- 받은 에이전트 테스트는 `tests/<agent>/`에 무수정 이식 후 통과 확인(Mac/3.11).
- 어댑터 신규 테스트(`tests/adapters/test_atlassian.py`, `tests/ingestion/test_crawler.py`,
  `tests/ingestion/test_sync.py`): vendored 출력 → `PageObject` 매핑 정합, ACL 합성, `is_acl_missing`→
  `INVALID_ACL`, 첨부 매핑, Chunking Queue 메시지 형식·라우팅 키(fake pika), `raw_*` 적재(fake Mongo),
  Delta since 필터, 3중 삭제(soft_delete/cascade), Reconciliation 멱등성.
- vendored 코드 자체의 단위 테스트는 작성하지 않는다(원본 책임). 우리는 **어댑터 경계만** 검증한다.

### 6) 검증 (이 샌드박스 제약 명시)

- 이 샌드박스는 Python 3.10이라 `StrEnum` 사용 코드의 pytest 실행이 불가하다 →
  여기서는 **ruff / py_compile / 정적 분석까지만** 수행하고, **전체 테스트(`./scripts/verify.sh`)·push는
  사용자가 Mac(3.11)에서** 수행한다. 커밋까지만 둔다(push 인증 정보 없음).

### 7) TBD — 협의 결과(ADR 0003 으로 해소)

- ✅ **ACL 모델 — 결정됨 (api-spec v2.4/v2.5)**: page-level `allowed_groups`/`allowed_users` 채택(Admin
  Key on), `space:{key}` 합성은 Admin-Key-OFF 폴백(ADR 0003 항목 1, ADR 0002 `space:` prefix).
  `_synthesize_acl`/`synthesize_space_acl` ↔ rag `build_acl_filter` 가 owning seam. 더 이상 결정 대기 아님.
- **`access_token`/`cloud_id` 전달 경로**(Auth Server→BFF→Ingestion): **ingestion↔rag 합의 불필요 —
  Auth/BFF 소관**(ADR 0003 "합의 불필요"). `CrawlRequest`/Settings placeholder 로 진행하며 두 레포 간
  결정 대상이 아니다.

### 8) 완료 기준

- 두 에이전트 패키지가 루트에 무수정 vendoring + `pyproject` include/제외 반영, vendored 테스트 통과.
- `AtlassianSourceAdapter`가 `DocumentSourceAdapter` 3개 메서드 충족, `crawler.run_full_crawl` mock
  end-to-end(Space→페이지→`raw_*` 적재→Chunking Queue 발행) 통과(featureI-2 완료 기준 충족).
- Sync 어댑터로 Delta Sync + 3중 삭제 흐름 mock 통과(featureI-5 완료 기준 충족).
- `./scripts/verify.sh`(Mac/3.11) 통과, `docs/db-schema.md`(`raw_pages`/`raw_attachments`
  스키마)·`docs/architecture.md`·`docs/ai/working-log.md` 갱신, 토큰·자격증명 미포함 확인.

---

## 진행 규칙 (요약)

1. feature 단위로만 작업한다. 다음 feature는 새 세션 또는 `/clear` 후 시작한다.
2. 테스트 케이스 정리 → 실패 테스트 작성 → 최소 구현 → 테스트 통과 순서를 지킨다.
3. 완료 후 `./scripts/verify.sh`(format → lint → test)를 실행한다.
4. `git diff`로 변경 범위를 확인하고 커밋한다.
5. 외부 의존성(Confluence/Qdrant/Mongo/RabbitMQ)은 fake/mock으로 대체해 테스트한다.

---

## RAG 레포 공유 자산 메모

- 복사 자산(schemas/chunker/embedder/embedding/vector_store/indexer/adapters/storage)은 RAG 레포와
  origin이 같다. 공통 계약(Qdrant payload / embedding_cache 키 / ACL 필드)을 바꾸면 RAG 레포도 갱신 필요.
- 장기적으로 공유 패키지 분리 여부 검토(현재는 복사 유지).

---

## featureI-5b — 3중 삭제 동기화 트리거 배선 (soft_delete wiring) — FR-005  📋 진행 중 (2026-06-04)

> **배경**: ADR 0003 항목 4로 soft_delete **능력**(payload `is_deleted`, store `soft_delete_by_page_id`/
> `soft_delete_by_attachment_id`, rag 검색 `must_not(is_deleted=true)`)은 이미 도입됐다. 그러나 실제로
> `soft_delete_by_*` 를 **호출하는 트리거가 없다** — `run_delta_sync` 는 `deleted_candidate_page_ids` 를
> 확인 대기 목록으로 surface 만 하고, Trash API/Webhook 경로는 미구현, store 를 소유한 Sync Worker 도
> 없다. 본 feature 는 ADR 0003 항목 4가 "Sync Worker 운영 wiring 책임"으로 남긴 그 배선을 구현한다.

- **작업 목표**: 3중 삭제 동기화의 3개 트리거(Delta `deleted_candidate` / Confluence Trash API /
  실시간 Webhook)를 단일 soft-delete 적용 seam 으로 모아 Qdrant `is_deleted=true` 를 set 한다.
  Reconciliation(주 1회 ghost, hard delete)은 무수정 보존 — 본 feature 와 직교한다.
- **브랜치**: `feat/#21/soft-delete-triggers`
- **근거**: 요구사항정의서 FR-005(3중 삭제 동기화), `docs/adr/0003-ingestion-rag-shared-contracts.md`
  항목 4, `docs/architecture.md`, `app/ingestion/sync.py` 기존 주석(Delta=surface only, Trash/Webhook=TBD),
  `docs/db-schema.md` §2.7/§1.2.

### 설계 — 단일 funnel + Worker(store 소유) + 3 트리거 소스

- **`app/ingestion/soft_delete.py` (신규)** — 외부 의존 0 seam.
  - `SoftDeleteStore` Protocol(`soft_delete_by_page_id`/`soft_delete_by_attachment_id`만) — store 결합 최소화.
  - `SoftDeleteResult`(frozen) — `soft_deleted_page_ids`/`_attachment_ids` + `failed_*`(id별 격리 집계).
  - `apply_soft_deletes(*, store, page_ids=(), attachment_ids=()) -> SoftDeleteResult` — dedup+정렬 결정론,
    id별 try/except 격리(한 건 실패가 전체 중단 금지). 3개 트리거가 모두 이 함수로 수렴.
- **`app/ingestion/workers/sync_worker.py` (신규)** — store 를 소유하고 3 트리거를 오케스트레이션.
  - `SyncWorkerDeps`(store: SoftDeleteStore, trash_source: TrashSource | None).
  - `apply_delta_deletions(result: DeltaSyncResult, *, confirm=False)` — **확인 게이트 보존**: 기본
    `confirm=False` 면 적용 안 함(후보 surface 만, 기존 정책 무변경). `confirm=True` 일 때만
    `apply_soft_deletes(page_ids=result.deleted_candidate_page_ids)`.
  - `run_trash_sync()` — `trash_source.list_trashed_ids()` → `apply_soft_deletes`(주1회→1시간 안전망).
  - `handle_webhook_event(event)` — 실시간 삭제 이벤트 1건 → `apply_soft_deletes`.
- **`app/adapters/confluence_trash.py` (신규)** — Trash 소스 seam.
  - `TrashedIds`(pages/attachments set) + `TrashSource` Protocol(`list_trashed_ids`).
  - `FakeTrashSource`(테스트) + `ConfluenceTrashSource`(실 — Confluence `?status=trashed` 조회,
    transport 주입으로 단위테스트. **vendored 무수정** — 본 어댑터가 직접 호출).
- **`app/api/webhook_routes.py` (신규)** — `POST /ml/confluence/webhook`.
  - `parse_confluence_delete_event(payload) -> WebhookDeleteEvent | None`(순수 함수) — Confluence
    webhook 페이로드(page_removed/page_trashed/attachment_removed 등)에서 page_id/attachment_id 추출.
  - 라우트: 파싱 → `deps.sync_worker.handle_webhook_event` → unwrapped data(`{"softDeleted": {...}}`)
    또는 4필드 에러 봉투. 인증은 BFF 책임(api-spec NOTE — 본 앱 미들웨어 미추가).
- **`app/api/deps.py` (수정)** — `IngestDeps` 에 `sync_worker: SyncWorker` 추가, `build_ingest_deps`
  에서 store(현 PoC fake)·trash_source 로 조립.
- **`app/api/main.py` (수정)** — webhook 라우터 include. 기존 `/ml/ingest` 라우트 무변경.
- **`app/ingestion/workers/__init__.py`/`app/storage/__init__.py`** — export.

### 수정하지 않을 파일

- `app/ingestion/sync.py` 의 `reconcile_deletions`(주1회 ghost, hard delete) — 무수정 보존.
- `app/storage/qdrant_client.py`/`qdrant_fake.py` 의 `soft_delete_by_*`(ADR 0003 항목4 — 이미 있음) — 재사용만.
- 공유 자산(schemas/chunker/embedder/embedding/vector_store/indexer) — 변경 불필요.
- vendored `data_*_agent/**` — 무수정(Trash 는 본 어댑터가 직접 호출).

### 예상 영향 범위

- 신규 모듈 4개 + DI 3곳 확장. 기존 `/ml/ingest` 계약·Delta surface 기본동작 무변경(confirm 기본 False).
- soft_delete 는 set_payload(보존 삭제)라 hard delete·검색 인덱스에 파괴적 영향 없음.

### 테스트 방법 (외부 의존성 fake/mock)

- `test_soft_delete.py` — dedup·정렬·id별 격리(일부 실패), 빈 입력, page/attachment 혼합.
- `test_sync_worker.py` — Delta confirm gate(False=무적용/True=적용), trash sync(FakeTrashSource),
  webhook 이벤트(page/attachment) → `FakeQdrantPoolStore.is_deleted` 검증, 멱등성.
- `test_confluence_trash.py` — fake transport 로 `?status=trashed` 페이지네이션·파싱.
- `test_webhook_route.py` — 파서 순수 함수(이벤트 유형별), 라우트(dependency_overrides) softDeleted 응답·
  잘못된 페이로드 4필드 에러.
- 검증: ruff/format/py_compile(샌드박스), 전체 pytest·`verify.sh` 는 Mac/3.11.

### 완료 기준

- 3개 트리거가 모두 `apply_soft_deletes` 로 수렴해 `FakeQdrantPoolStore.is_deleted=true` set, id별 격리.
- Delta 확인 게이트 보존(기본 무적용). webhook 라우트 end-to-end(fake) 통과.
- `docs/db-schema.md`(삭제 트리거 흐름)·`docs/architecture.md`·`docs/ai/working-log.md` 갱신. 토큰 미로깅.
- `./scripts/verify.sh`(Mac/3.11) 통과 후 커밋·push.

### TBD(후속)

- 실 RabbitMQ/스케줄러로 `run_trash_sync` 주기 구동 + webhook 실엔드포인트 배포(featureI-7c 인프라 의존).
- Delta 확인 게이트의 운영 정책(자동 confirm 여부)은 운영 합의 후 토글.

## featureI-7c: Data Ingestion Worker completion event 발행 정합 (FR-006 연동)

- **작업 목표**: Data Ingestion Worker가 수집 성공/실패 시 BFF가 소비하는 completion queue로
  credential 정보를 배제한 완료 이벤트를 발행해, BFF의 `Admin Key deactivate` 트리거가 안정적으로 동작하도록 한다.
  BFF 현재 기준 계약은 `default exchange("")` + `routingKey=lina.admin.ingest.completion` + durable queue
  (`lina.admin.ingest.completion`, DLQ `lina.admin.ingest.completion.dlq`)이다.
- **브랜치**: `feat/#1/rabbitmq-completion-event`
- **근거 문서**: [rabbitmq-completion-event-guide.md](/Users/idayeon/skala/final-project/ingestion-deploy/docs/ai/rabbitmq-completion-event-guide.md), `docs/architecture.md`, `app/AGENTS.md`,
  `docs/conventions.md`, BFF consumer 설정(`@RabbitListener(queues = "${lina.admin.ingest.rabbitmq.completion-queue}")`).

### 구현 계획

1. **MQ 계약 확정(실행 규칙 반영)**
   - 기존 큐 이름 유지:
     - completion queue: `lina.admin.ingest.completion`
     - DLQ: `lina.admin.ingest.completion.dlq`
     - BFF consumer: `@RabbitListener(queues = "${lina.admin.ingest.rabbitmq.completion-queue}")`
 - Data Ingestion Worker publish 방식:
     - exchange: 기본 exchange(`""`)
     - routingKey: `lina.admin.ingest.completion`
     - deliveryMode: `PERSISTENT`
     - contentType: `application/json`
     - 주의: routing key 인자 생략 시 기본값(`lina.admin.ingest.completion`)으로 발행되므로, 운영에서
       별도 라우팅 키를 사용하려면 설정/구성 값(`ingest_completion_routing_key`) 일괄 정합 필요.
   - 향후 named exchange 필요 시 BFF에 completion-exchange/completion-routing-key/binding 추가가 선행되어야 함.

2. **completion event DTO/스키마 정의**
   - Data Ingestion Worker 측에서 BFF와 동일 schema로 정의:
     - 필수: `jobId`, `adminUserId`, `mode`, `status`, `completedAt`
     - 허용 status: `COMPLETED`, `FAILED`
     - 금지: `accessToken`, `refreshToken`, `cloudId`, `adminApiToken`, `adminEmail`
   - 예시 payload:
     - `{"jobId":"job-...","adminUserId":"admin-account-id","mode":"full","status":"COMPLETED","completedAt":"2026-06-11T08:00:00Z","errorCode":null,"message":"done"}`

3. **발행 시점 및 실패 처리**
   - ingest job consume → auth-server에서 admin credential 조회 → Confluence 수집 수행
   - 수집 성공 시 `status=COMPLETED` event publish
   - 수집 실패 시 `status=FAILED` event publish(민감정보 없는 `errorCode`, `message` 포함)
   - 수집 실패라도 **반드시 FAILED 발행**하여 BFF deactivate 경로가 항상 동작하도록 보장.
   - 이벤트 publish 실패는 worker 공통 재시도/에러정책 적용(로그 남김, DLQ 전략은 큐 정책과 정합).

4. **영속성 및 확정성**
   - publish message는 persistent로 발행.
   - 가능 시 publisher confirm 활성화로 broker 반영 여부 확인.

### 수정 대상 파일

- `app/ingestion/workers/ingestion_worker.py` — 수집 예외 경로 포함 completion event 발행 seam 추가.
- `app/ingestion/workers/publisher.py` 또는 공통 MQ publisher 유틸 — completion 이벤트 publish 경로 분기
  추가(전송 옵션/영속성 보장/오류 분기 통합).
- `app/api/ingest_completion.py` 또는 completion event DTO 위치(동일 계약 정의 위치) — payload schema
  정합 정의.
- `app/config.py` — completion queue/DLQ/라우팅 기본값 정비(현재 설정 경로와 충돌 없도록).
- `tests/ingestion/test_ingestion_worker.py` — 성공/실패 발행 시나리오, payload 필드 금지 검증 추가.
- `tests/ingestion/test_completion_events.py`(신규) — completion event 스키마/영속성/예외 시퀀스 단위 테스트.
- `tests/integration/`(가능 시) — Testcontainers로 completion queue 적재 end-to-end 검증.

### 테스트 항목 (반드시 추가)

- [ ] 수집 성공 시 `COMPLETED` 이벤트 publish
- [ ] 수집 실패 시 `FAILED` 이벤트 publish
- [ ] payload에서 `accessToken/refreshToken/cloudId/adminApiToken/adminEmail` 미포함
- [ ] `jobId` 또는 `adminUserId` 누락 시 publish 금지 또는 명시적 실패 처리
- [ ] publish 메시지 `deliveryMode=PERSISTENT` 검증
- [ ] Testcontainers 통합 테스트 가능 시 completion queue 실입력 검증
- [ ] BFF 연동 e2e: completion queue/DLQ 프로비저닝 → publish/consume → deactivate API 호출/실패 시 DLQ 이동

### 완료 기준

- 실패/성공 모든 경로에서 completion 이벤트를 BFF 계약에 맞게 발행.
- RabbitMQ 메시지는 기본 exchange + routingKey(`lina.admin.ingest.completion`) + persistent로 발행됨.
- status는 `COMPLETED` 또는 `FAILED`만 발생, credential 필드가 payload에 절대 누출되지 않음.
- 최소 단위 테스트 + 통합(가능 시) 추가 후 `./scripts/verify.sh` 통과.
- `docs/db-schema.md`/`docs/architecture.md` 변경이 필요한 경우 동반 갱신.
- 작업 로그에 변경 근거(특히 MQ 계약 확정 항목) 기록.

## Milestone F — auth-server 내부 API 호출 보안 정합 (INGESTION → auth-server)

### featureI-8: `/internal/auth/admin-confluence-credential` 호출에 `X-Internal-Api-Key` 적용  📋 계획

- **작업 목표**: `adminUserId` 기반 credential lookup 경로를 auth-server 내부 API
  (`/internal/auth/admin-confluence-credential?adminUserId=...`)로 명시하고, 이 경로에서만
  `X-Internal-Api-Key`를 주입해 안전하게 credential 조회를 수행한다.
- **브랜치**: `feat/#<이슈번호>/internal-auth-api-key`
- **근거 문서**: `docs/architecture.md`, `app/AGENTS.md`, `app/config.py`(Settings), `app/api/routes.py`
  (`adminUserId` 전달 계약), `docs/api-spec.md` §2-5(권고, 운영 계약), `docs/runbooks/auth-server-oauth-smoke.md` §10(curl).
  ※ auth-server 구현 검증이 필요할 경우 참조: `backend-template/backend/auth-server` 내부
  `InternalApiKeyFilter.java`/`SecurityConfig.java`/`InternalCredentialController.java`/`InternalCredentialService.java`/`application.yml`.

#### 1) 구현 범위

1. `app/config.py`에 INTERNAL API KEY/내부 auth-server base 설정 추가
   - 권장: `internal_api_key`(SecretStr), `auth_server_base_url` 또는 `auth_server_internal_path_prefix`.
   - 운영 모드에서 `internal_api_key` 미설정 시 fail-fast 또는 startup 경고 정책을 명시.
   - 운영 키 주입 값은 `INTERNAL_API_KEY`로 통일.
2. `app/ingestion/bootstrap.py`에 `/internal/auth/admin-confluence-credential` 호출 클라이언트/팩토리 추가
   - `adminUserId` 조회 시에만 `X-Internal-Api-Key` 주입.
   - `adminUserId`는 realm 접두사 포함 전체값 사용(예: `712020:91b5...`).
   - 공개 API 경로(`/internal` 외부 경로)엔 헤더 미주입 보장.
   - 응답은 `{"accessToken","cloudId","siteUrl","expiresAt"}`만 사용. `refreshToken`은 반환되지 않음.
3. `app/api/deps.py`와 `app/ingestion/workers/ingestion_worker.py` DI seam 정리
   - `IngestDeps`에 `credential_lookup` 주입/전달 명세를 명확화.
   - 기존 `run_ingest_job_from_payload(..., credential_lookup=...)` 호출 경로와 일치.
4. `app/ingestion/workers/ingestion_worker.py` 로직 강화
   - `credential_lookup` 응답과 예외를 계약별로 구분:
     - 정상(200): `accessToken`, `cloudId` 바인딩(후속 Confluence는 `https://api.atlassian.com/ex/confluence/{cloudId}/...` + `Authorization: Bearer {accessToken}`, admin-key 필요 시 `Atl-Confluence-With-Admin-Key: true`).
     - 400: `adminUserId` 누락 파라미터(요청 점검).
     - 401: 헤더 미설정/미스매치 분기(작업 중단/복구 포인트).
     - 403: admin 아님(credential 매칭 대상 확인).
     - 404: user 없음 혹은 OAuth 선로그인 필요(재작업 전 선행 로그인 필요).
     - 5xx/네트워크: 분류된 재시도 정책 또는 job fail-fast 정책 적용 여부 기록
   - 내부 API 실패가 외부 API 경로까지 오염되지 않도록 로그 마스킹/민감정보 노출 제한 유지.
     - `siteUrl`은 후속 요청 URL 구성(`api.atlassian.com/ex/confluence/{cloudId}`) 및 관측 지표로 사용 가능.
5. 운영 점검 가시성 및 알림
   - 401 발생 분기(헤더 누락/미스매치)와 재시도 임계치 기반 알림 포인트를 runbook 체크리스트에 반영.
   - staging/prod 배포 체크리스트에 내부 키 동기화(Namespace/Secret 키명/값) 항목 추가.
   - 키 값 협의 항목 추가(권장안: auth-server가 `INTERNAL_API_KEY` 생성 후 Worker와 공유).
   - 대안인 NetworkPolicy만으로 경량화할 경우 auth-server `ROLE_INTERNAL` 완화가 필요하므로, 기본은 헤더 필수 정책 유지.
   - 완료 조건: `INTERNAL_API_KEY` 미정합 2건(누락/불일치) 발생 시 운영 알림 규칙 정의.

#### 2) 테스트 계획

- [x] 단위: internal 전용 client에서 헤더 주입/분기 분리 단위 테스트
  (`/internal` vs 공개 API, adminUserId/헤더 없음/헤더 포함).
- [x] 단위: `_resolve_runtime_credentials` 상태코드별 분기(400/401/403/404) 경로 테스트.
- [x] 단위: 401에서 누락/미스매치 메시지 분리와 로그 마스킹 정책 테스트.
- [x] 통합: 키 일치 시 200 응답(`accessToken/cloudId/siteUrl/expiresAt`) 수신 후 수집 흐름 연동 테스트.
- [x] 통합: 키 누락/오류/미스매치 시 401, 400, 403, 404 응답 재현 및 job 실패/경고 정책 검증.
- [x] 통합: `siteUrl` 반영한 Confluence API URL 구성 경로(`.../ex/confluence/{cloudId}/...`) 검증.
- [x] 통합: `credential_lookup` 미주입/빈 값 시 fail-fast 또는 경고 정책 검증(운영/PoC mode 분기).

#### 3) 완료 기준

- `adminUserId` 조회 경로에서 internal 헤더 분리 주입이 일관되게 적용됨.
- 401 누락/미스매치 로그가 운영 판단 가능하게 구분됨.
- 기존 `/internal` 외 경로는 헤더 오염 없이 동작.
- `docs/ai/featureI-8-internal-auth-key-runbook.md`, `docs/ai/working-log.md`(또는 운영 runbook) 갱신 반영.
- 관련 단위/통합 테스트 추가 후 `./scripts/verify.sh` 통과.
- `/internal` 응답 200/400/401/403/404 동작이 status별로 추적 가능하고, `refreshToken` 미반환 계약을 보장.
- 키 동기화 합의 항목(생성 주체, 주입 채널, Secret 키명/네임스페이스) 잔여 이슈 폐기.

#### 4) ML 팀 전달용 핸드오프 템플릿 (복붙)

- Worker는 auth-server 내부 API 호출 시 반드시 `X-Internal-Api-Key` 헤더를 보내야 함.
- adminUserId는 realm 접두사 포함 전체값(예: `712020:...`) 사용.
- 요청: `GET /internal/auth/admin-confluence-credential?adminUserId=...`
- 응답: `{accessToken, cloudId, siteUrl, expiresAt}` + 200.
- 상태 처리: 401(헤더/키 미설정/오류), 400(adminUserId 누락), 403(ADMIN 아님), 404(사용자 없음/미로그인).
- 운영: `INTERNAL_API_KEY`를 auth-server와 Worker가 동일하게 주입하고, NetworkPolicy 단독 우회는 기본 미채택.
