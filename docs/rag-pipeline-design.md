# RAG Pipeline 설계 (구현 참조)

이 문서는 RAG 파이프라인 설계서(`DESIGN-RAG-PIPELINE-2026-001 v0.2.2`, 작성자 최태성)를
구현 관점에서 정리한 저장소 내 참조 문서다. 원본 설계서·기획서(`PLAN-CONF-RAG-2026-002 v2.1.6`)가
Source of Truth이며, 충돌 시 원본을 우선한다. 설계 변경 시 이 문서와 `docs/architecture.md`·
`docs/db-schema.md`·`docs/api-spec.md`를 함께 갱신한다.

---

## 1. 서비스 개요

척척학사(LINA, Linked Intelligence & Navigation Agent) — Confluence 사내 문서 본문 +
첨부 파일(PDF/Word/Excel) 텍스트를 대상으로 한 RAG 기반 사내 지식 검색 챗봇.
본 저장소는 그중 **RAG 파이프라인** 컴포넌트를 담당한다.

검색 단위는 **사용자 단위** — 사용자가 접근 권한을 가진 모든 페이지·첨부를 단일 인덱스에서 검색한다.

## 2. 설계 원칙

정확성 우선(근거 없는 답변 금지), 검증 가능성(출처 동봉), 비용 효율(Agent/Pipeline 분리 + 검증 게이팅),
접근 제어(권한 밖 문서는 검색 자체 불가), 확장성(Multi-Pool + 2단계 검색 + Worker 수평 확장).

## 3. 컴포넌트 분류 체계

모든 컴포넌트는 `[Agent]` / `[Pipeline]` / `[Storage]` 중 하나로 명시한다.

| 분류 | 정의 | 특성 |
|---|---|---|
| Agent | LLM이 입력을 분석해 판단 | 비결정론적. 프롬프트 튜닝·토큰 비용 모니터링 대상 |
| Pipeline | 사전 정의 규칙으로 결정론적 처리 | 동일 입력 → 동일 출력. 단위 테스트로 품질 보증 |
| Storage | 데이터 저장·조회 | 상태 보유. 인프라 모니터링 대상 |

## 4. Document Source Adapter (파이프라인 경계)

RAG 파이프라인은 데이터 공급원에 직접 결합하지 않는다. 입구에 `DocumentSourceAdapter`
인터페이스를 두고, 공급원이 무엇이든 동일한 표준 `PageObject` 스트림을 반환하도록 강제한다.
전환 시 바뀌는 것은 **어댑터 1개 클래스 + `source.type` 설정 1줄**뿐이다.

인터페이스 메서드: `fetch_pages()` (전체/증분, 첨부 텍스트 포함), `list_active_ids()`
(Reconciliation용 페이지·첨부 ID 집합), `watch_changes()` (실시간 변경 스트림).

PoC 어댑터 (본 저장소 구현 대상):

- `JsonFixtureSourceAdapter` — `samples/*.json`(Atlassian-Python-API 응답 포맷)을 읽는다. 로컬 개발·테스트용.
- `AtlassianSourceAdapter` — `atlassian-python-api`로 Confluence REST를 직접 호출한다. 상세는 `docs/atlassian-api.md`.

> **백엔드 미구축 반영** — 원 설계서는 PoC에서 백엔드가 Atlassian → MongoDB로 적재한 mock을
> RAG가 읽는 구조였으나, 백엔드(BFF)가 아직 없어 **ML 파이프라인(본 저장소)이 Atlassian REST API를
> 직접 호출**한다(Atlassian API 명세서·기획서 §6.6 정합). OAuth 인증·`access_token` 관리는
> Authorization Server(Spring)가 담당하며, 본 저장소는 발급된 토큰·`cloudid`를 전달받아 사용한다.
> 첨부 파일 다운로드·텍스트 추출(PDF→PyMuPDF, Word→python-docx, Excel→openpyxl/pandas)도
> 본 저장소(`AtlassianSourceAdapter` 또는 첨부 분석기 단계)가 담당한다.

## 5. Ingestion 파이프라인

입력: 표준 `PageObject` (본문 + `attachments[]` + ACL + 메타). 모든 단계는 RabbitMQ Worker로 분리.

| # | 컴포넌트 | 분류 | 책임 |
|---|---|---|---|
| 3.3 | 문서 분석기 | Agent | 스페이스 최초 등록 시 샘플 5~10건을 GPT-4o-mini에 전달해 `doc_type` 판별. MySQL `space_doc_type_cache`에 캐싱(스페이스당 1회). confidence < 0.6 또는 실패 시 `operation` 기본값 |
| 3.3.B | 첨부 파일 분석기 | Pipeline | mime/확장자 → `attachment_type ∈ {pdf,docx,xlsx,csv}`, 텍스트 유효성 검증(200자 미만/반복 80%↑ 스킵), 부모 페이지 메타·ACL 상속, `source_type="attachment"` 부여 |
| 3.4 | Adaptive Chunker | Pipeline | `doc_type`/`attachment_type`별 청킹 → `chunks[]`. 상세: `docs/chunking-strategy.md` |
| 3.5 | Dual Embedding | Pipeline | Dense(multilingual-e5-large, 1024차원) + Sparse(BM25). Qdrant Named Vector로 동일 Point에 저장. MongoDB `embedding_cache`로 멱등성 확보 |
| 3.6 | Multi-Pool Vector Store | Storage | Qdrant 3개 Collection(title/content/label pool). 모든 Point에 ACL Payload + `source_type` 동봉 |
| 3.7 | 삭제 동기화 | Pipeline | 3중 전략. PoC는 Reconciliation(주 1회)만 활성, 운영 전환 시 Trash API Sync(1시간)·Webhook(즉시) 추가 |

문서 유형 6종: 장애대응 / 운영매뉴얼 / FAQ / 회의록 / ADR / 트러블슈팅.
처리 결과는 MongoDB `ingestion_jobs`에 `(page_id, attachment_id, stage, status, ...)`로 기록.

## 6. Query 파이프라인

입력: 사용자 질문 + JWT. 모든 검색에 ACL 필터가 시스템 단에서 강제 적용된다.

| # | 컴포넌트 | 분류 | 책임 |
|---|---|---|---|
| 4.2 | ACL Pre-filtering | Pipeline | JWT → `user_id`/`groups` → Qdrant 필터(`should`= allowed_groups/allowed_users OR 결합). `@enforce_acl` 데코레이터로 ACL 없는 검색 호출을 시스템 단에서 거부 |
| 4.3 | 멀티턴 히스토리 관리자 | Agent | GPT-4o-mini. 새 주제/연속/검색스킵 판단, 히스토리 보존·삭제. 최근 5턴 한도 |
| 4.4 | 질의 라우터 | Agent | GPT-4o-mini **단일 호출**로 Intent Router(4종 의도) + Query Rewriter(2~3개 쿼리) + Filter Builder(메타 필터 + Pool 가중치) 동시 수행. Function Calling 강제 |
| 4.5 | Multi-Pool Hybrid Search | Pipeline | 3개 Pool 병렬 검색. Pool 내부 RRF(dense+bm25, k=60) → Pool 가중 합산 → Top-20 |
| 4.5 | Cross-Encoder 재순위화 | Pipeline | `ms-marco-MiniLM-L-12`로 Top-20 → Top-5. 5위 점수 < 0.30이면 Top-3로 축소 |
| 4.6 | 답변 생성기 | Agent | GPT-4o(라우터 결정 시 4o-mini), 의도별 프롬프트 + Top-5 컨텍스트, SSE 스트리밍, Function Calling(`answer_text`, `sentence_to_citations[]`) |
| 4.7 | 답변 검증 | Pipeline + Agent | 1단계 규칙 매칭(엔티티/수치/코드 토큰, Mecab) → FLAG → 2단계 GPT-4o-mini 평가자(`SUPPORTED`/`NOT_SUPPORTED`) |
| 4.8 | 응답 포맷터 | Pipeline | 검증된 답변·출처·검증 결과를 UI JSON으로 변환 (`docs/api-spec.md` 참조) |

4종 의도: 장애대응 / 운영가이드 / 정책절차 / 이력조회. 의도별 Pool 가중치(Title/Content/Label):
장애대응 0.4/0.5/0.1, 운영가이드 0.2/0.7/0.1, 정책절차 0.5/0.4/0.1, 이력조회 0.3/0.3/0.4.

## 7. 핵심 데이터 구조

### 7.1 PageObject (Ingestion 입력 — 백엔드와 동결)

`page_id`(필수, `CONF-PAGE-{n}`), `space_key`, `title`, `body_html`, `labels[]`, `ancestors[]`,
`version_number`(필수, 동일 시 색인 스킵=멱등성), `last_modified`(ISO 8601), `allowed_groups[]`(필수),
`allowed_users[]`(필수), `webui_link`(필수), `attachments[]`.

`attachments[]` 객체: `attachment_id`, `filename`, `mime_type`, `extracted_text`,
`extracted_format`(`raw_text`|`sheet_serialized`), `file_size_bytes`, `download_url`,
`parent_page_id`, `last_modified`.

> ACL 산출 결과(`allowed_groups`·`allowed_users`)가 정책 적용 후에도 **모두 비면**(공개 sentinel `"*"`·
> page-level·`space:{key}` 폴백 중 어느 것도 부여되지 않은 경우) **ACL 누락 오류**(`INVALID_ACL`)로 색인
> 스킵한다(보안 안전 측). restriction 이 비어 있을 뿐인 페이지는 `allow_authenticated` 정책이 `"*"`를
> 부여하므로 스킵 대상이 아니다(아래 결정 참조). 첨부는 부모 페이지 ACL 상속.

> **✅ ACL 결정 완료 (api-spec v2.4/v2.5)** — 운영 ACL은 청크별 `allowed_groups`/`allowed_users`
> Payload로 적재하며, page-level **read restriction API**(`/rest/api/content/{id}/restriction/
> byOperation/read`, Admin Key 헤더로 조회)에서 산출한다. 빈 권한은 `allow_authenticated` 정책의
> 공개 sentinel `"*"`로 적재한다. PoC fixture/Admin Key 미사용 경로는 `space:{key}` 합성을
> fallback으로 유지(ADR 0002). 상세: `docs/api-spec.md` §1-4/§2-2, `docs/atlassian-api.md`,
> `docs/db-schema.md`.

### 7.2 Chunk 메타데이터

청크 메타데이터·청크 분할 규칙은 `docs/chunking-strategy.md`, Qdrant Payload 스키마는
`docs/db-schema.md` 참조.

## 8. 예외 / Fallback 정책 (요약)

| 단계 | 예외 | 처리 |
|---|---|---|
| 문서 분석기 | LLM 실패 / confidence < 0.6 | `doc_type=operation` 기본값 |
| 첨부 분석기 | 미지원 mime / 암호화 PDF / 저품질 | `UNSUPPORTED_ATTACH_TYPE` / `ATTACH_ENCRYPTED` / `LOW_QUALITY_ATTACH` 기록 후 스킵 (본문 색인은 정상) |
| Chunker | HTML 파싱 실패 | plain text + H2 분할 fallback, `PARTIAL_PARSE` 기록 |
| ACL Pre-filter | JWT 추출 실패 | 401 반환, 검색 미수행 |
| 라우터 | LLM 타임아웃(3초) | 안전 기본값(의도=운영가이드, 원본 쿼리, 빈 필터) |
| 검색 | 후보 0건 | LLM 미호출, "권한 범위 내 문서를 찾지 못했습니다" 표준 응답 (환각 차단) |
| 재순위화 | Top-5 최고 점수 < 0.20 | 저신뢰 분기 — 출처는 '참고용'으로 제시 + 경고 배지 |
| 답변 생성 | 타임아웃(15초) / Rate Limit / 스키마 위반 | 지연 메시지 / 4o-mini 다운그레이드 / 1회 재시도 후 plain text |
| 검증 | `NOT_SUPPORTED` 비율 > 50% | 답변 차단, 저신뢰 응답 대체, 긴급 알림 |

## 9. 기술 스택

Python 3.11 · LangGraph 0.2.x / LangChain 0.3.x · `openai>=1.30` ·
Qdrant(Vector DB) · MongoDB(문서·잡·임베딩 캐시) · MySQL(`space_doc_type_cache`) · RabbitMQ(Worker 큐) ·
FastAPI + SSE · Dense: `intfloat/multilingual-e5-large`(1차 후보) · Sparse: BM25(KoNLPy Mecab / Kiwi) ·
Cross-Encoder: `cross-encoder/ms-marco-MiniLM-L-12` · LLM: GPT-4o(생성) / GPT-4o-mini(라우터·검증·히스토리·문서분석기) ·
첨부 추출: PyMuPDF·pdfplumber / python-docx / openpyxl·pandas · 토큰: tiktoken `cl100k_base`.

## 10. KPI (기획서 §10)

| 항목 | 최소 기준 | 목표 기준 |
|---|---|---|
| 정보 검색 소요 시간 | 3분 이내 | 30초 이내 |
| 검색 정확도 Precision@3 | 60% 이상 | 75% 이상 |
| 환각 비율 | 25% 이하 | 15% 이하 |
| 응답 시간 P95 | 8초 이내 | 5초 이내 |
| 사용자 만족도 (피드백) | 긍정 60% 이상 | 긍정 80% 이상 |
| 서비스 가용성 | 95% 이상 | 99% 이상 |

> **환각 비율 측정 방식 (2026-05-26 확정)** — 헤드라인 환각 비율은 **표준 RAG
> faithfulness** 로 측정한다: 답변 문장이 **검색된 전체 top-k 근거**에 의해 뒷받침되는지를
> 검증하고, 미근거 문장 비율을 환각률로 본다(RAGAS faithfulness / TruLens groundedness 정의).
> feature17c-26 전수 실측: **delivered 0.81% / answerable 1.91%** — 목표 15%·이상 8% 모두 충족.
>
> 인용한 청크 기준(per-cited)으로 재는 **citation precision(출처 정밀도, delivered 19.4%)** 은
> "환각"이 아니라 **출처 귀속 정확도** 보조 지표로 분리 보고한다(인용 번호 오기 = 출처 정밀도
> 문제이지 날조 아님). 평가 스크립트(`run_evaluation.py`)는 두 지표를 모두 산출한다(이원 측정).
> 외부 기획서 FR-009/FR-010 의 per-cited 정의는 본 측정 방식 정합으로 갱신 권고
> (산출물 `구현_결과_보고서_527_RAG검색품질_성능최적화_리포트_v0.3.0.docx` §4 근거).

## 11. PoC 범위 / 향후 확장

PoC 포함: 본문 텍스트 + 첨부 파일(PDF/Word/Excel) 텍스트 색인.
PoC 제외(향후 확장): 이미지·도형·다이어그램 등 비텍스트 콘텐츠(멀티모달), API Gateway/BFF/인증
플로우, 인프라 구성, 관리자 UI, Valkey 캐시 계층.
