# app/CLAUDE.md — Data Ingestion Pipeline 전용 규칙

이 문서는 Data Ingestion Pipeline 영역(`app/`, `tests/`)에서만 적용되는 규칙을 정의한다.
작업 시 루트 `CLAUDE.md`의 공통 규칙을 먼저 적용하고, 이 문서의 규칙을 추가로 따른다.

설계 기준 문서: `docs/rag-pipeline-design.md`(시스템 설계 §3 수집·§5 임베딩·§7 스키마),
`docs/chunking-strategy.md`, `docs/atlassian-api.md`(Confluence 수집 API), `docs/db-schema.md`,
`docs/architecture.md`.

> chunker·embedder·schemas·storage·adapters 는 RAG 레포에서 복사한 공유 자산이다. 공통 계약
> 변경 시 RAG 레포와의 분기 영향을 먼저 설명한다(루트 `CLAUDE.md` 참조).

---

## 1. 컴포넌트 분류 규칙 (Agent / Pipeline / Storage)

모든 컴포넌트는 `[Agent]` / `[Pipeline]` / `[Storage]` 중 하나로 분류하고, 모듈 docstring과
클래스/함수 주석에 명시한다. 이 분류는 비용 모니터링·테스트 전략의 기준이다.

- **Agent** — LLM을 호출해 판단하는 컴포넌트(예: 문서 분석기 doc_type 판별). 비결정론적. 프롬프트는 역할·입력·출력·제약을 포함하고, 변경 시 의도·기대 효과·부작용 가능성을 문서화한다.
- **Pipeline** — 사전 정의 규칙으로 결정론적으로 처리(첨부 추출·Adaptive Chunker·Dual Embedding 입력 구성 등). 동일 입력 → 동일 출력. 반드시 단위 테스트로 회귀를 보호한다.
- **Storage** — 데이터 저장·조회(MongoDB·Qdrant·MySQL). 외부 의존성은 어댑터/클라이언트 계층으로 분리한다.

`app/` 디렉토리별 분류는 각 패키지의 `__init__.py` docstring을 따른다.

## 2. 파이프라인 단계 분리

- 수집 단계를 명확히 분리한다: 수집(Crawl/Delta) → 첨부 텍스트 추출 → 문서/첨부 분석 → Adaptive Chunker → Dual Embedding → Multi-Pool Store → 삭제 동기화.
- 각 단계는 RabbitMQ 큐(Ingestion / Attachment / Chunking / Embedding)로 연결되며, Worker는 단일 책임을 갖는다(다음 큐로만 인계).
- 한 단계가 다른 단계의 책임(예: 추출이 청킹을, 청킹이 임베딩을)을 침범하지 않는다.
- 질의/응답(Query) 단계는 별도 저장소(`../rag`)의 책임이며 본 레포에서 다루지 않는다.

## 3. 보안·정확성 (절대 규칙)

- **ACL을 색인 시점에 정확히 적재한다.** 페이지·청크 payload에 `allowed_groups`/`allowed_users`를 동반 적재해야 Query 단계의 ACL Pre-filtering이 동작한다. ACL 정보가 전혀 없는 PageObject·청크는 색인하지 않는다 (`INVALID_ACL`).
- ACL 접두 규약은 `docs/adr/0002-acl-prefix-convention.md`를 따른다.
- 출처·메타가 누락된 색인을 만들지 않는다. 수집 결과 무효 시 재시도 또는 DLQ 보류한다.
- Secret·API Key·Confluence `access_token`은 코드·로그·테스트 픽스처·메시지 페이로드에 포함하지 않는다. 설정은 `app/config.py`에서 환경 변수로 주입한다(요구사항정의서 §6.1).

## 4. 결정론·멱등성

- Pipeline 컴포넌트는 결정론을 유지한다. `chunk_id`는 SHA1(`page_id`+`chunk_index`+`attachment_id`)로 계산하며 임의 UUID를 쓰지 않는다.
- 동일 `chunk_id` + `version_number`는 재임베딩·재upsert를 스킵한다 (`embedding_cache`).
- Delta Sync는 `version`/`updatedAt` 비교로 변경·삭제 페이지만 처리한다(전체 재색인 금지).
- 청킹·임베딩 설정 변경 시 변경 이유·기대 효과를 `docs/ai/working-log.md`에 기록한다.

## 5. LLM 호출 규칙

- 모델 라우팅을 준수한다: 문서 분석기(doc_type 판별)는 GPT-4o-mini.
- 구조화 출력이 필요한 호출(문서 분석기)은 Function Calling으로 스키마를 강제한다.
- LLM 호출에는 타임아웃과 Fallback을 정의한다.
- 실험성 프롬프트·모델은 production path에 직접 연결하지 않는다.

## 6. 테스트 규칙

- 구현 전 테스트 케이스를 먼저 정리한다 (`docs/ai/workflow.md`의 테스트 우선 절차).
- Pipeline 컴포넌트(Chunker, 첨부 추출, 임베딩 입력 구성, 멱등성 판정, 동기화 판정)는 Unit Test를 필수로 작성한다.
- Agent 컴포넌트(문서 분석기)는 LLM 응답을 mock/fake로 대체하고, 입출력 스키마 계약과 Fallback 분기를 테스트한다.
- Confluence·Qdrant·MongoDB·MySQL·RabbitMQ 등 외부 의존성은 테스트에서 mock/fake로 대체한다.
- 버그 수정 시 재현 테스트를 먼저 작성한다.

## 7. 평가·관측 규칙

- 청킹·임베딩 설정 변경 후 색인 품질(청크 수·오류 건수·멱등성)을 확인한다.
- 수집/청킹/임베딩 잡의 진행·결과는 `import_jobs`(MongoDB)에 단계별 상태로 기록한다.
- Worker 커스텀 메트릭(잡 카운터·지연·실패율)은 Prometheus로 관측한다(아키텍처 Logging&Monitoring).

## 8. 코딩 컨벤션

- Python 3.11 기준. `docs/conventions.md`의 표준 주석 블록을 주요 모듈·클래스·public 함수에 작성한다.
- 외부 호출(Confluence, Qdrant, MongoDB, MySQL, RabbitMQ)은 어댑터/클라이언트/Worker 계층으로 분리하고 청킹·임베딩 로직에서 직접 호출하지 않는다.
- 데이터 모델은 `app/schemas`의 Pydantic 모델로 정의하고 계층 간 dict를 그대로 전달하지 않는다.
