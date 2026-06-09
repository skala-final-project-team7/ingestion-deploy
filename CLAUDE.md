# CLAUDE.md

이 문서는 Claude Code가 **Data Ingestion Pipeline** 저장소에서 따라야 하는 최상위 공통 규칙을 정의한다.
RAG Pipeline 저장소(`../rag`)와 분리된 독립 배포 단위이며, 공통 모델(`app/schemas`)·청킹·임베딩
자산은 RAG 레포에서 복사해온 것이다(2026-05-26 분리). 공통 자산 변경 시 RAG 레포와의 정합에 유의한다.

---

## 절대 규칙

- 사용자가 요청하지 않은 대규모 리팩토링을 하지 않는다.
- 담당 범위를 벗어난 파일은 수정하지 않는다.
- Confluence 수집 계약, RabbitMQ 큐/라우팅 키, DB Schema, ACL 적재 흐름을 변경할 경우 관련 문서를 함께 수정한다.
- Secret, Token, Credential, `.env` 파일을 생성하거나 커밋하지 않는다. **특히 Confluence OAuth access token은 로그·메시지 페이로드에 남기지 않는다(요구사항정의서 §6.1 보안).**
- 테스트 실패 상태로 작업을 완료했다고 보고하지 않는다.
- 임시 코드, 우회 코드, 불필요한 TODO, 디버깅용 로그를 남기지 않는다.
- 기존 아키텍처 의존 방향을 임의로 바꾸지 않는다.
- 공통 자산(`app/schemas`·`app/ingestion/chunker`·`embedder`)을 수정해야 할 경우, RAG 레포와의 분기 영향을 먼저 설명한다.
- 작업 범위가 불명확하면 기존 코드와 문서를 먼저 확인하고, 추측으로 구현하지 않는다.

---

## 프로젝트 문서

작업 전 아래 문서를 확인한다.

- Data Ingestion Pipeline app/ 전용 규칙: `app/CLAUDE.md`
- Claude Code 작업 플로우: `docs/ai/workflow.md`
- 팀 공통 프롬프트 템플릿: `docs/ai/prompt-templates.md`
- 아키텍처 문서: `docs/architecture.md`
- 시스템 설계서(수집 §3 / 임베딩 §5 / 스키마 §7): `docs/rag-pipeline-design.md`
- 청킹 전략: `docs/chunking-strategy.md`
- Confluence 수집 API: `docs/atlassian-api.md`
- 코딩 컨벤션: `docs/conventions.md`
- DB 스키마: `docs/db-schema.md`
- ACL 접두 규약(ADR): `docs/adr/0002-acl-prefix-convention.md`
- 현재 작업 Plan: `docs/ai/current-plan.md`

> 요구사항 원본(FR-001 수집 / FR-002 첨부 텍스트 추출 / FR-003 청킹 / FR-004 임베딩 색인)은
> 외부 `요구사항정의서`를 기준으로 한다. 수집·동기화 계약 변경 시 `docs/architecture.md`·
> `docs/db-schema.md`를 함께 갱신한다.

---

## 작업 시작 규칙

- 작업 전 반드시 작업 목표와 담당 영역을 확인한다.
- 구현 전 Plan을 먼저 작성한다(`docs/ai/current-plan.md`).
- Plan에는 다음을 포함한다: 작업 목표 / 수정 대상 파일 / 수정하지 않을 파일 / 예상 영향 범위 / 테스트 방법 / 완료 기준 / 문서 수정 필요 여부.
- 작업 범위가 커지면 기능을 작은 단위(feature)로 나눈다.
- 불확실한 부분은 기존 코드, 문서, 테스트를 먼저 확인한다.

---

## 담당 영역별 확인 규칙

### Data Ingestion Agent (FR-001) / Data Sync Agent 작업

- `docs/architecture.md` (수집·동기화 흐름, RabbitMQ 큐)
- `docs/conventions.md`
- `docs/db-schema.md` (`raw_pages` / `raw_attachments` / `import_jobs`)
- `app/adapters/` (DocumentSourceAdapter 계약)

### 첨부 텍스트 추출기 (FR-002) 작업

- `docs/architecture.md`
- `docs/db-schema.md` (`raw_attachments.extracted_text` — 별도 attachment_texts 미사용)
- `app/ingestion/extractor/`

### Chunking (FR-003) / Embedding (FR-004) 작업

- `docs/architecture.md`
- `docs/conventions.md`
- `docs/db-schema.md` (Qdrant payload / `embedding_cache`)
- `app/ingestion/chunker/`, `app/ingestion/embedder/`, `app/ingestion/{embedding,vector_store,indexer}.py`

### RabbitMQ Worker 작업

- `docs/architecture.md` (큐·라우팅 키·DLQ)
- `app/ingestion/workers/`

---

## 구현 규칙

- 기존 코드 스타일과 폴더 구조를 우선적으로 따른다.
- 새로운 패턴을 도입하기보다 기존 패턴을 확장한다.
- Adapter, Worker, Pipeline(청킹/임베딩), Storage 클라이언트의 책임을 섞지 않는다.
- 외부 API 호출(Confluence), DB 접근, 메시지 큐 처리 등 I/O 작업은 명확한 계층으로 분리한다.
- 비즈니스 로직은 테스트 가능한 형태로 작성한다(외부 의존성은 fake/mock 으로 대체).
- 예외 처리는 공통 예외 처리 구조를 따른다. 수집 실패는 재시도 또는 DLQ 보류로 처리한다.
- 로그는 문제 원인 추적에 필요한 정보만 남기고, 민감 정보(토큰·자격증명)는 남기지 않는다.

---

## 테스트 규칙

- 기능 구현 전 Acceptance Criteria와 Test Case를 먼저 정리한다.
- 핵심 도메인 로직(청킹·임베딩 입력·멱등성·동기화 판정)은 Unit Test를 작성한다.
- 수집/큐 계약 변경 시 메시지 형식 검증 테스트를 작성한다.
- 외부 의존성(Confluence/Qdrant/Mongo/RabbitMQ)은 fake/mock 으로 대체해 테스트한다.
- 버그 수정 시 재현 테스트를 먼저 작성한다.
- 테스트 실패 원인을 무시하거나 테스트를 삭제해서 통과시키지 않는다.

---

## 검증 명령

작업 완료 전 아래 명령을 실행한다.

```bash
./scripts/format.sh
./scripts/lint.sh
./scripts/test.sh
./scripts/verify.sh
```

일부 명령이 실패하면 실패 원인과 해결 여부를 작업 결과에 기록한다.

---

## 작업 완료 규칙

작업 완료 전 반드시 다음을 확인한다.

- 구현 범위가 요청 범위를 벗어나지 않았는가
- 관련 테스트가 추가 또는 수정되었는가
- lint, format, test가 통과했는가
- 수집/큐/스키마 변경 시 `docs/architecture.md`·`docs/db-schema.md`가 수정되었는가
- 토큰·자격증명·`.env`가 커밋에 포함되지 않았는가
- 불필요한 로그, 주석, 임시 코드가 남아 있지 않은가
- `git diff` 기준으로 의도하지 않은 변경이 없는가

---

## Git 커밋·푸시 규칙

> 이 레포(`ingestion`)와 `../rag`는 **독립된 git 저장소(형제 관계)** 다. 커밋·푸시는 **각 레포에서
> 따로** 수행한다. 한 작업이 두 레포를 모두 건드리면, 같은 change-set를 각 레포에서 개별 브랜치·
> 개별 커밋·개별 푸시로 처리한다(한쪽 커밋이 다른 쪽을 포함하지 않는다).

- **커밋·푸시는 사용자가 수행한다.** Claude는 브랜치명·커밋 메시지 **초안만 제안**하고, `git commit`/
  `git push`를 임의로 실행하지 않는다.
- **브랜치 분리**: change-set마다 전용 브랜치를 만든다. 형식 `<type>/#<이슈번호>/<기능-이름>`
  (`type` = `feat` / `fix` / `docs` / `refactor`). 다른 작업 브랜치 위에 새 change-set를 쌓지 않는다.
- **커밋 전 검증**: `./scripts/verify.sh`(format → lint → test)를 통과시킨다. 문서만 변경한
  change-set도 실행해 무영향을 확인한다. 실패 시 원인·해결 여부를 작업 결과에 기록한다.
- **스테이징 확인**: `git add -A` 전에 `git status --short`로 의도한 파일만 변경됐는지 확인한다
  (`git diff`로 의도하지 않은 변경이 없는지도 본다).
- **비밀정보 금지**: 토큰·자격증명·`.env`가 스테이징/커밋에 포함되지 않았는지 확인한다(절대 규칙).
- **공유 자산 변경 동기화**: `app/schemas`·`app/ingestion/{chunker,embedder,...}`·`app/adapters`·
  `app/storage` 등 `../rag`와 공유하는 자산을 바꾸면, 같은 change-set로 양 레포를 동일하게 갱신하고
  ADR로 기록한다(소유권·동기화 절차는 `docs/adr/0003-ingestion-rag-shared-contracts.md` 항목 2 참조).
- **커밋 메시지**: 제목 한 줄(`<type>(<scope>): <요약>`) + 빈 줄 + 본문 bullet(무엇을·왜). 예시는 아래.

```bash
# 예시 — change-set 단위 (이 레포에서 단독 수행)
git checkout -b feat/#<이슈번호>/<기능-이름>
./scripts/verify.sh                 # format → lint → test 통과
git status --short                  # 의도한 파일만 확인
git add -A
git commit -m "feat(ingestion): <요약>

- <무엇을 했는지>
- <왜 / 영향>"
git push --set-upstream origin feat/#<이슈번호>/<기능-이름>
```

---

## 세션 운영 원칙

- 1 change-set = 1 session을 원칙으로 한다.
- 큰 기능은 milestone 단위로 나누어 작업한다.
- 세션이 길어지면 현재 상태를 요약하고 새 세션에서 이어간다.
- 작업 중 중요한 결정은 문서에 남긴다.
- 내부 추론 과정에 의존하지 말고 Plan, Diff, Test Result, Command Log를 기준으로 검증한다.
