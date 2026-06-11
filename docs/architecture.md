# Data Ingestion Pipeline — 아키텍처

척척학사(LINA) RAG 챗봇의 **데이터 수집 파이프라인**. Confluence 문서·첨부를 수집해
첨부 텍스트 추출 → Adaptive Chunking → Dual Embedding 색인까지 수행하고, 변경/삭제를 주기적으로
동기화한다. RAG Pipeline(질의/응답, `../rag`)과 **분리된 독립 배포 단위**이며, 사용자 요청 트래픽과
격리된 RabbitMQ 기반 비동기 파이프라인으로 동작한다.

근거: 요구사항정의서 §2.0(Multi-Agent)·§2.2(FR-001~004)·§3(데이터 요구사항), 아키텍처 다이어그램(07).

---

## 1. 구성 요소

```
                 ┌─────────────── Confluence (Atlassian REST) ───────────────┐
                 │                                                            │
   (관리자/스케줄러 트리거)                                            (Delta 1시간 / Webhook / 주1회 Reconciliation)
                 ▼                                                            ▼
        ⑤ Data Ingestion Agent (FR-001)                          ⑥ Data Sync Agent
        Full Crawl: Space→homepage→descendants                    변경·삭제 페이지 식별
        본문·첨부(PDF/Word/Excel)·메타·ACL 수집                     → 재수집 / chunk 재생성 / upsert
                 │  raw_pages / raw_attachments (MongoDB)          삭제 3중 전략(Trash/Webhook/Reconcile)
                 ▼
        [RabbitMQ] Ingestion Queue
                 │
                 ▼
        첨부 텍스트 추출기 (FR-002)  ── raw_attachments.extracted_text (MongoDB)
        PDF/Word/Excel → 텍스트(이미지·도형 제외)
                 │  [RabbitMQ] Chunking Queue
                 ▼
        Adaptive Chunker (FR-003)  ── 문서 분석기(Agent) + Adaptive Chunker(Pipeline)
        본문 6유형(장애/운영/FAQ/회의록/ADR/트러블슈팅) + 첨부 3유형
                 │  [RabbitMQ] Embedding Queue
                 ▼
        Dual Embedding 색인 (FR-004)
        Dense(multilingual-e5-large 1024d) + Sparse(BM25)
                 │
                 ▼
        Qdrant Multi-Pool (Title / Content / Label)  +  embedding_cache (MongoDB, 멱등성)
```

- 각 단계 Worker(Ingestion / Attachment Extractor / Chunking / Embedding)는 EKS에서 독립 스케일링한다.
- RabbitMQ는 Quorum Queue 권장. 실패 페이지/첨부는 재시도 또는 DLQ 보류.
- 잡 진행/결과는 MongoDB `ingestion_jobs`에 단계별 상태로 기록한다.

## 2. 패키지 매핑

| 컴포넌트 | FR | 패키지/모듈 | 상태 |
|---|---|---|---|
| Data Ingestion Agent (Full Crawl) | FR-001 | `data_ingestion_agent`(lina-ai-agents 설치) ↔ `app/adapters/atlassian.py` + `app/ingestion/crawler.py` | 통합 완료(featureI-6) |
| Data Sync Agent (Delta/삭제) | FR-005 | `data_sync_agent`(lina-ai-agents 설치) ↔ `app/ingestion/sync.py`(`run_delta_sync`) ↔ `app/api/routes.py`(`/ml/ingest` mode=delta) | 통합 완료(featureI-6) + HTTP delta 라우팅 배선 / Reconciliation 복사 유지 |
| Sync Worker (soft-delete 트리거) | FR-005 | `app/ingestion/workers/sync_worker.py` + `app/ingestion/soft_delete.py` + `app/adapters/confluence_trash.py` + `POST /ml/confluence/webhook` | featureI-5b — Delta 확인 게이트·Trash API·Webhook → `apply_soft_deletes`(`is_deleted`). 주기 구동·실행 loop 는 featureI-7c |
| 첨부 텍스트 추출기·다운로더 | FR-002 | `app/ingestion/extractor/` + `app/ingestion/attachment_downloader.py` | 다운로더(`Noop`/`Http`, download_url→local_path) 배선 + bootstrap 실 branch 주입(`build_attachment_downloader`) — 운영 첨부 청킹은 파일 기반 `chunk_attachment`(local_path 직접 읽기). `extractor/` 는 bytes 입력용 추출 seam(featureI-3, 프로덕션 배선 없음). 텍스트는 `raw_attachments.extracted_text` 에 보존(별도 attachment_texts 미사용 — db-schema §2.7). **실 어댑터 첨부 메타(download_url) 수집은 후속** |
| 문서·첨부 분석기 | FR-003 | `app/ingestion/document_analyzer.py`(신규 [Agent]) · `attachment_analyzer.py`(복사 Pipeline) | 문서 분석기[Agent] 구현(featureI-4b, GPT-4o-mini+캐싱). 첨부 분석기 복사 |
| Adaptive Chunker | FR-003 | `app/ingestion/chunker/` | 복사 완료 (본문 6유형 + 첨부 3유형) |
| Dual Embedding | FR-004 | `app/ingestion/embedder/`, `embedding.py` | 복사 완료 |
| Multi-Pool 색인 | FR-004 | `app/ingestion/vector_store.py`, `indexer.py` | 복사 완료 |
| RabbitMQ Workers | — | `app/ingestion/workers/` (`publisher.py`·`consumer.py`·`chunking_worker.py`) | Publisher/Consumer + Chunking+Embedding Worker(featureI-4, 단일 토폴로지) 구현 |
| 문서 공급원 어댑터 | FR-001 | `app/adapters/` (`atlassian.py` 구현) | JsonFixture(복사) + Atlassian(vendored 연결, featureI-6) |
| Raw Store | FR-001 | `app/storage/raw_store.py` | 구현(featureI-6) — `raw_pages`/`raw_attachments` 적재 + `get_page` 조회 |
| 파이프라인 조립(PoC) | — | `app/ingestion/pipeline.py` | in-process crawl→chunk→index 합성(featureI-7). **운영은 큐로 분리** |
| Fake Qdrant(PoC/테스트) | — | `app/storage/qdrant_fake.py` | `FakeQdrantPoolStore`(featureI-7) — 외부 Qdrant 없이 적재·삭제동기화 검증 |
| 의존성 부트스트랩 | — | `app/ingestion/bootstrap.py` | Settings 기반 조립(PoC Fake / 실 어댑터, featureI-7b). pika 실행 loop·CLI 는 featureI-7c |
| 저장소 클라이언트 | — | `app/storage/` | 복사 (Mongo/Qdrant/chunk_lookup/jobs) |

## 3. 외부 의존성 / 저장소

- **Confluence (Atlassian REST)**: Page/첨부/ACL 수집. 토큰은 수집 단계에서만 사용하며 로그·메시지에 미수집.
- **RabbitMQ**: Ingestion / Attachment / Chunking / Embedding 큐(라우팅 키 기반).
- **MongoDB**: `raw_pages`, `raw_attachments`(`extracted_text` 포함), `ingestion_jobs`, `embedding_cache`, `audit_logs`. (별도 `attachment_texts` 컬렉션은 두지 않음 — db-schema §2.7.)
- **MySQL**: `space_doc_type_cache`(문서 분석기 캐싱).
- **Qdrant**: Multi-Pool(Title/Content/Label) Named Vector Collection.

## 4. RAG 레포와의 경계 / 공유 자산

- `app/schemas`(PageObject/Chunk/enums), `app/ingestion/chunker`·`embedder`·`embedding.py`·
  `vector_store.py`·`indexer.py`, `app/adapters`, `app/storage` 는 RAG 레포에서 **복사**한 자산이다.
- Qdrant payload 스키마·`embedding_cache` 멱등성 키·ACL 필드(`allowed_groups`/`allowed_users`)는
  RAG의 검색 단계와 **계약을 공유**하므로, 변경 시 양 레포(`docs/db-schema.md`)를 함께 갱신한다.
- 향후 공통 자산을 공유 패키지로 분리할지 여부는 `docs/ai/current-plan.md`에서 결정한다.

## 5. 외부 에이전트 통합 (featureI-6 — 배포 레포는 설치형)

- Data Ingestion Agent(FR-001)·Data Sync Agent(FR-005)는 별도 `ai-agent` 레포가 소유한
  독립 패키지다. **본 배포 레포는 vendoring 하지 않고** `lina-ai-agents @ git+…@v0.1.1`
  외부 의존성으로 설치한다(top-level 패키지명 `data_ingestion_agent`/`data_sync_agent`
  동일 — import 무변경. `INTEGRATION.md` §1~§3). 에이전트 테스트·lint 는 ai-agent 레포
  소관이라 본 레포 `pyproject.toml` 에 별도 노출/제외 설정이 없다.
  (개발 원본 `ingestion` 레포는 동일 패키지를 저장소 루트에 무수정 vendoring 한다.)
- ingestion 본체는 에이전트 코드를 **직접 호출하지 않고** 얇은 어댑터로만 연결한다:
  - `app/adapters/atlassian.py` `AtlassianSourceAdapter` → vendored `run_full_crawl_workflow`
    를 in-process 블랙박스 호출 후 산출 `ProcessedDocument` 를 표준 `PageObject` 로 변환.
  - `app/ingestion/sync.py` `run_delta_sync` → vendored `run_data_sync_workflow` 를 호출 후
    `ChangedDocument` 를 `PageObject` 로 변환, 삭제 후보 page_id 집계.
- vendored 스키마(중첩 space/page/body)와 ingestion 계약(평면 `PageObject`)이 어긋나는 부분
  (ACL·labels·ancestors·attachments 미산출)은 **어댑터에서 변환·합성**한다(vendored 무수정 보존).
- 통합 갭(추측 구현 금지): page-level ACL 실연동(모델은 확정 — api-spec v2.4/v2.5·ADR 0003 항목 1;
  vendored MVP 가 read restriction 미산출), credential 전달(`adminUserId` 기반 auth-server 조회 —
  api-spec §2-2/§2-5), 첨부 수집. crawl 단계 `ingestion_jobs` stage 는 ADR 0003 항목 3 으로 해소됨.
  상세는 `docs/ai/current-plan.md`.
