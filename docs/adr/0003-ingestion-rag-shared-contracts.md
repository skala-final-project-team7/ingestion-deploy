# 0003. ingestion↔rag 공유 계약 합의 결정서

- 상태: 채택 (항목 1·2·5는 PoC 확정 / 항목 3·4는 **적용됨 2026-05-26**, 사용자 승인)
- 날짜: 2026-05-26
- 작성자: 최태성
- 적용 범위: `ingestion` 레포(Data Ingestion Pipeline)와 `rag` 레포(RAG Pipeline) **양쪽 동일 기록**.
  본 ADR은 두 레포 `docs/adr/0003-*.md`에 동일 내용으로 복제한다.

> **갱신 노트 (api-spec v2.4/v2.5, 2026-06-09)**: 공유 ACL 계약은 page-level read restriction
> 기반 `allowed_groups`/`allowed_users` + 빈 권한 시 `allow_authenticated` 공개 sentinel `"*"`
> (rag `build_acl_filter`가 모든 principal 에 `"*"` 주입)로 확정되었다(`docs/api-spec.md`
> §1-4/§2-2). `space_key` 는 payload 식별/표시 필드로 유지되고, `space:{key}` ACL 합성은 PoC
> fixture/Admin Key 미사용 시 fallback 이다. Admin Key 말소는 v2.5에서 RabbitMQ completion
> event(ML 발행 → BFF consumer → auth-server deactivate)로 확정.

## 배경

`ingestion`의 `app/schemas`, `app/ingestion/{chunker,embedder,embedding,vector_store,indexer}`,
`app/adapters`, `app/storage`는 `rag`에서 복사해 온 자산이다(2026-05-26 분리, ingestion CLAUDE.md
머리말). 따라서 **Qdrant payload 스키마 · `embedding_cache` 멱등성 키 · `chunk_id` 생성 규칙 ·
ACL 필드(`allowed_groups`/`allowed_users`/`space_key`)** 는 ingestion의 색인 단계와 rag의 검색
단계가 "계약을 공유"한다. 한쪽을 바꾸면 다른 쪽 런타임이 깨질 수 있다.

ingestion 진행 과정(`docs/ai/current-plan.md`·`working-log.md`)에서 다음 미해결(TBD)이 양 레포
공유 계약으로 식별되었다. 본 ADR은 각 항목을 코드/문서 근거로 결정하고 영향과 후속을 명시한다.

### 현황 검증(2026-05-26, 코드 diff 기준)

- 공유 자산 `app/schemas/{enums,chunk,page_object}.py`, `app/ingestion/{vector_store,indexer,
  embedding}.py`, `app/storage/{qdrant_client,jobs,mongo_cache}.py`, `app/adapters/{base,
  json_fixture}.py`는 두 레포에서 **바이트 단위로 동일**하다.
- 유일한 의도적 분기는 `app/ingestion/sync.py` — ingestion이 `run_delta_sync`(Delta Sync)를
  **비파괴 추가**했고, rag는 `reconcile_deletions`만 보유한다. 공유 함수 `reconcile_deletions`의
  시그니처·동작은 두 레포 동일.
- `chunk_id = SHA1("{page_id}:{chunk_index}:{attachment_id or ''}")` (`app/schemas/chunk.py:make_chunk_id`).
- `embedding_cache` 멱등성 키 = `(chunk_id, version_number)` (`app/ingestion/embedding.py:should_skip_embedding`).
- Qdrant payload 스키마 = `app/ingestion/vector_store.py:build_point_payload`(두 레포 동일).
- rag 검색 ACL = `app/query/acl.py:build_acl_filter` → `{"should":[{allowed_groups any groups},
  {allowed_users any [user_id]}]}` (OR), `app/storage/qdrant_client.py`에서 다른 메타 필터와 AND 결합.

---

## 항목 1 — ★ ACL 모델 (가장 중요한 교차 계약)

### 결정

**(A) `space_key` 합성 모델을 PoC 계약으로 확정**한다. 페이지 단위 권한(content restrictions)
도입(대안 B)은 PoC 범위에서 채택하지 않으며, 도입 시 별도 ADR로 다룬다.

구체적으로:

- ingestion은 수집 시 페이지의 `allowed_groups`를 `["space:{space_key}"]`로 합성한다
  (`app/adapters/atlassian.py:synthesize_space_acl`, `app/adapters/json_fixture.py:_synthesize_acl`).
  `allowed_users`는 빈 목록.
- rag는 검색 시 `app/query/acl.py:build_acl_filter`가 JWT `groups` 클레임을 `allowed_groups`에
  OR 매칭한다. **BFF는 JWT `groups`를 `space:{key}` 형식으로 채워야 한다**(예:
  `groups=["space:CLOUD","space:CCC"]`). 이 prefix 컨벤션은 이미 **ADR 0002**로 동결돼 있다.
- payload는 결정 후에도 `space_key` + `allowed_groups` + `allowed_users`를 **모두 인덱싱**한 채로
  둔다(대안 B 전환 여지 보존).

### 근거

- `docs/atlassian-api.md` "⚠ ACL 미해결 사항": 제공된 Atlassian API 명세에 페이지 단위 권한 API가
  없고 `DATA-03`(사용자 접근 가능 Space 목록)만 존재. 기획서 §6.2/§6.5도 ACL을 "스페이스 접근
  권한"으로 기술. 샘플 데이터에도 ACL 필드 없음. → 대안 B는 명세 외 추가 작업이 필요해 PoC 불가.
- 현재 양 레포 코드가 이미 대안 A로 정합 동작한다: 합성 seam(`_synthesize_acl`/`synthesize_space_acl`)
  과 검색 seam(`build_acl_filter`)이 모두 격리돼 있어, 모델 교체 시 이 두 지점만 바꾸면 된다
  (ingestion `app/CLAUDE.md` §3 "ACL 필드 모델 교체 가능" 원칙, rag `db-schema.md` §1.4).
- ADR 0002가 `space:{key}` prefix를 이미 동결 — 본 결정은 그 위에 "PoC ACL 입도 = 스페이스 단위"를
  명시 확정하는 것.

### 영향

- 런타임 변경 **없음**. 두 레포 현재 동작이 곧 본 결정이다.
- 입도(granularity)는 스페이스 단위 — 같은 스페이스 내 페이지별 접근 제어는 PoC 범위 밖(문서 명시).
- rag 검색 정확도는 BFF의 JWT `groups` 형식 정합에 의존(ADR 0002). 어긋나면 모든 쿼리가
  `RETRIEVAL_EMPTY`/빈 결과. → BFF 영역(아래 "합의 불필요" 참조).

### 후속 작업

- 양 레포 `docs/db-schema.md` §1.4 "ACL 필드 모델 미해결" 절을 "PoC 결정: 대안 A — ADR 0003 참조"로
  갱신(모델 교체 여지 문구 유지). 양 레포 `docs/atlassian-api.md` ACL 미해결 절에 본 ADR 포인터 추가.
- 대안 B(content restrictions) 도입 시: `build_acl_filter`와 `_synthesize_acl`만 교체 + 재색인,
  별도 ADR. `space:` prefix는 ADR 0002대로 유지하거나 별도 ADR로 변경.

### 승인 필요 여부

**불필요** — 문서 정합 갱신만. 런타임 무변.

---

## 항목 2 — Qdrant payload 스키마 / `embedding_cache` 키 / `chunk_id` 규칙: 소유권·분기 관리

### 결정

- **소유권(owning source) = `rag` 레포.** 세 계약(payload 스키마·`embedding_cache` 키·`chunk_id`
  규칙)의 정의는 rag가 owning하며, ingestion은 복사본을 미러링한다.
- **변경 절차(동기화 정책):** 이 계약을 바꾸려면 (1) rag에 먼저 변경 + ADR, (2) 같은 change-set로
  ingestion 복사본을 동일하게 갱신, (3) 양 레포 동시 배포 + (스키마/키 변경 시) 재색인. 한쪽만
  바꾸는 것을 금지한다.
- **분기(divergence) 관리:** 공유 파일은 바이트 동일을 유지한다. 불가피한 분기(현재 `sync.py`처럼
  한쪽 전용 함수 추가)는 **비파괴 추가(additive)** 로만 허용하고, 공유 함수의 시그니처·동작은
  양쪽 동일하게 보존한다. 분기 발생 시 해당 파일 상단 주석 또는 본 ADR "분기 등록부"에 기록한다.

### 근거

- 자산이 rag → ingestion 방향으로 복사됐다(ingestion CLAUDE.md). 정의의 원본이 rag이므로 owning을
  rag로 두는 것이 자연스럽다.
- 현재 diff 검증상 공유 자산은 `sync.py`(additive 분기)를 제외하고 모두 바이트 동일 → 정책이 이미
  사실상 지켜지고 있다.
- payload 필드(`allowed_groups`/`allowed_users`/`source_type` 등)는 검색 정확도와 직결돼 임의 제거
  금지(양 레포 `db-schema.md` §"운영 주의" 정합).

### 분기 등록부 (2026-05-26 기준)

| 파일 | 상태 | 비고 |
|---|---|---|
| `app/ingestion/sync.py` | ingestion이 `run_delta_sync` additive 추가 | 공유 `reconcile_deletions`는 동일. Delta Sync는 ingestion 전용(수집 영역) |
| 그 외 공유 자산 전체 | 바이트 동일 | schemas / vector_store / indexer / embedding / qdrant_client / jobs / mongo_cache / adapters |

### 영향 / 후속

- 런타임 변경 없음. 본 항목은 거버넌스(소유권·절차) 결정.
- 후속: 향후 공유 자산 PR 리뷰 시 본 절차를 체크리스트로 사용. 장기 전략은 항목 5 참조.

### 승인 필요 여부

**불필요** — 거버넌스/문서 결정.

---

## 항목 3 — `IngestionStage` enum에 수집 단계(crawl/ingest) 추가 여부

> **상태: 적용됨 (2026-05-26, 사용자 승인).** 아래 "결정"의 `CRAWL` 추가안을 양 레포에 동시
> 반영했다. `app/schemas/enums.py`에 `CRAWL = "crawl"` 추가(양 레포 바이트 동일), rag
> `tests/schemas/test_enums.py` 멤버셋 갱신, ingestion `crawler.run_full_crawl`에 optional `jobs`
> 주입으로 페이지별 CRAWL SUCCESS 기록 배선, 양 레포 `db-schema.md` §2.3 갱신. **배포 시 양 레포를
> 함께 올려야** 한다(아래 영향의 읽기 측 `ValueError` 위험).

### 현황

`app/schemas/enums.py:IngestionStage = analyze/chunk/embed/upsert/sync` (양 레포 동일). 수집(crawl)
단계 값이 없어 ingestion은 crawl 단계의 `ingestion_jobs` 기록을 **보류**하고 `CrawlResult`/
`DeltaSyncResult`를 잡 리포트로 대신 쓰고 있다(`working-log` 2026-05-26 featureI-6). 색인 종단
단계(`UPSERT`)는 enum에 존재해 기록 중이다.

### 결정 (제안 — 승인 대기)

**`IngestionStage`에 `CRAWL = "crawl"`(수집) 값 1개를 추가**할 것을 제안한다(`INGEST`는 `CRAWL`과
중복이라 추가하지 않음 — 수집=crawl로 단일화). 이로써 ingestion이 수집 단계 잡을 `ingestion_jobs`에
정식 기록할 수 있다.

단, 이는 **공유 enum 변경 = 런타임 계약 변경**이므로 아래 영향 검토 후 **사람 승인 시에만 적용**한다.
승인 전까지는 현행대로 `CrawlResult`/`DeltaSyncResult` 리포트를 유지한다.

### 영향

- enum은 `.value` 문자열로 직렬화돼 `ingestion_jobs.stage`에 저장된다(양 레포 `db-schema.md` §2.3).
- **읽는 쪽 위험:** rag 또는 관리자 대시보드가 `IngestionStage(value)`로 역파싱한다면, ingestion이
  새 값 `"crawl"`을 기록한 레코드를 **enum 정의가 갱신되지 않은 레포가 읽을 때 `ValueError`** 발생.
  → 따라서 **양 레포 enum을 동시에 갱신·배포**해야 안전(쓰기[ingestion] 전 읽기[rag] 선반영 권장).
- 관리자 대시보드 조회 API는 "별도 시스템 책임"으로 명시돼 있다(`db-schema.md` §2.3). 대시보드가
  stage를 화이트리스트로 필터링하면 새 값은 무시될 수 있으니 대시보드 담당과도 확인 필요.
- 추가 자체는 기존 값에 영향 없는 additive 변경.

### 후속 작업 (승인 시)

1. rag `app/schemas/enums.py`에 `CRAWL = "crawl"` 추가(읽기 측 선반영) + 동일 change-set로 ingestion
   복사본 갱신(항목 2 절차).
2. 양 레포 `db-schema.md` §2.3 `stage` 설명에 `crawl` 추가.
3. ingestion crawl 단계(`crawler.run_full_crawl`/sync)에 `ingestion_jobs` 기록 배선.
4. 양 레포 동시 배포.

### 승인 필요 여부

**필요** — 공유 enum 값 추가 + 양 레포 동시 갱신/배포. 본 ADR은 결정안만 기록하고 코드는 변경하지
않는다.

---

## 항목 4 — 삭제 동기화 의미론 (soft_delete 도입 여부)

> **상태: 적용됨 (2026-05-26, 사용자 승인).** 아래 "도입 시 계약 규약"을 양 레포에 동시 반영했다.
> 공유 `build_point_payload`에 `is_deleted: False` 추가, `QdrantPoolStore`에 `is_deleted` BOOL
> payload 인덱스 + 검색 결합 필터 `must_not(is_deleted=true)` + `soft_delete_by_page_id`/
> `soft_delete_by_attachment_id`(`set_payload`) 추가(모두 공유 `qdrant_client.py` — 양 레포 바이트
> 동일), ingestion `FakeQdrantPoolStore`에 동일 인터페이스, 양 레포 `db-schema.md` §1.2·§1.3 갱신.
> **hard delete(`delete_by_*`)는 그대로 보존**하며 soft/hard는 호출 측이 선택한다. legacy Point
> (필드 부재)는 미삭제로 간주된다(재색인 없이 후방 호환). **배포 시 양 레포를 함께 올려야** 하며,
> 기존 인덱스에 `is_deleted`를 채우려면 재색인(또는 일괄 `set_payload` 백필)이 필요하다.
>
> **후속(미적용)**: Delta Sync의 `deleted_candidate`/Trash/Webhook 경로를 `soft_delete_by_*` 호출에
> 실제로 배선하는 것은 store를 소유한 Sync Worker의 운영 wiring 책임으로 남긴다(능력은 도입됨).

### 현황

- 설계상 Delta Sync는 `deleted_candidate`를 surface하고(확정 삭제 아님, requires_confirmation),
  3중 삭제 동기화 중 Trash/Webhook 경로는 Qdrant payload `soft_delete`를 의도한다
  (`app/ingestion/sync.py` 주석, ingestion `current-plan` featureI-5).
- 그러나 **store에는 hard delete(`delete_by_page_id`/`delete_by_attachment_id`/`delete_by_chunk_id`)만**
  존재한다(`app/storage/qdrant_client.py`). Reconciliation(고스트 제거)은 hard delete를 수행한다.
- **rag 검색에는 soft-delete 필터가 전혀 없다** — `app/query/acl.py`·`qdrant_client.search`·
  `_chunk_from_search_hit` 어디에도 `is_deleted`/`soft_delete` 개념이 없음(grep 확인). payload 스키마
  (`build_point_payload`)에도 해당 필드 없음.

### 결정

**PoC는 hard delete 의미론을 유지**한다. soft_delete는 PoC 범위에서 **도입하지 않는다**. 도입이
필요해지면 아래 "도입 시 계약 규약"을 따르되 **사람 승인 시에만** 적용한다.

#### soft_delete 도입 시 계약 규약 (승인 시 적용할 사양 — 지금은 미적용)

- payload에 `is_deleted: bool`(기본 `false`) 필드 추가. ingestion이 삭제 확정 시 `true`로 upsert.
- rag 검색은 `is_deleted = true`를 **`must_not`으로 제외**하도록 `qdrant_client.search`의 결합 필터에
  추가(또는 ACL 필터와 함께 AND). `_chunk_from_search_hit` 복원에도 필드 반영.
- payload 인덱스에 `is_deleted` keyword 인덱스 추가(`db-schema.md` §1.3).
- 기존 인덱스 호환: legacy 포인트에 필드가 없으면 "미삭제"로 간주(필터를 `is_deleted = true`
  `must_not`으로 작성하면 필드 부재 시 자연히 통과).

### 근거

- 현재 코드가 hard delete만 지원하고 rag 검색에 필터가 없어, soft_delete는 **새 payload 필드 +
  rag 검색 필터 + 전체 재색인**이 동반되는 큰 변경이다(PoC 범위 초과).
- Trash API/Webhook 자체가 에이전트·본 레포 모두 MVP 제외 상태(`working-log` 2026-05-26)라 soft_delete
  를 구동할 입력 경로도 아직 없다.

### 영향 / 후속 (도입 시)

- payload 필드 추가 = 항목 2 소유권/절차 적용(rag owning, 양 레포 동시 + 재색인).
- rag 검색 필터 추가 = 검색 동작 변경(런타임).
- 양 레포 동시 배포 + 전체 재색인 필요.

### 승인 필요 여부

**필요** — payload 필드 추가 + rag 검색 필터 변경 + 전체 재색인 + 양 레포 동시 배포. 본 ADR은
규약만 기록하고 코드는 변경하지 않는다.

---

## 항목 5 — 공유 자산 장기 전략 (복사 유지 vs 공유 패키지 분리)

### 결정

**PoC/현 단계는 복사 유지**(항목 2의 소유권·동기화 절차로 관리). 공유 패키지 분리(예: 사내 PyPI 또는
git submodule로 `lina-shared` 추출)는 **다음 조건 충족 시 재검토**: (a) 공유 자산 분기가 잦아져 수동
동기화 비용이 커지거나, (b) 운영 배포에서 버전 불일치 사고가 발생하거나, (c) 제3 레포가 같은 자산을
요구할 때.

### 근거

- 현재 분기는 `sync.py`(additive) 1건뿐이고 나머지는 바이트 동일 → 수동 동기화 비용이 낮다.
- 패키지 분리는 빌드/버전/배포 파이프라인 변경을 수반해 PoC 단계 오버엔지니어링.
- ingestion `current-plan` "RAG 레포 공유 자산 메모"도 "장기적으로 공유 패키지 분리 여부 검토(현재는
  복사 유지)"로 동일 방향.

### 영향 / 후속

- 런타임 변경 없음. 분리 결정 시 별도 ADR(패키징·버전 정책·CI 동기화 검증).

### 승인 필요 여부

**불필요** — 전략/문서 결정.

---

## 합의 불필요 (ingestion↔rag 외 다른 영역 소관)

- **`access_token`/`cloudId` 전달 경로 (Authorization Server → BFF → Ingestion)** — Auth/BFF 영역.
  현재 두 레포 어디에도 Authorization Server/BFF 코드가 없다(rag는 JWT를 **발급/검증하지 않고 추출만**
  함 — `app/query/acl.py` 머리말 "JWT 서명 검증·발급은 BFF 책임"). ingestion은 `CrawlRequest`/Settings
  placeholder로만 다룬다. → **ingestion↔rag 합의 대상 아님.** Auth/BFF 담당 결정 사항.
- **JWT 발급·서명·`groups` 클레임 주입 자체** — BFF/Authorization Server 영역. 단, `groups`의 *형식*
  (`space:{key}`)은 ADR 0002로 이미 동결됐고 본 ADR 항목 1이 의존성을 명시한다.
- **관리자 대시보드 / 사용자·대화·피드백 데이터** — BFF/백엔드 영역(`db-schema.md` §2.3 "관리자
  대시보드 조회 API는 별도 시스템 책임"). 단, 대시보드가 읽는 `IngestionStage` enum *값*은 공유
  계약이라 항목 3에서 다룬다(읽기 측 동시 갱신 필요성).

---

## 사람 승인이 필요한 항목 요약 (런타임/배포 영향)

| 항목 | 제안 | 영향 | 적용 조건 |
|---|---|---|---|
| 3. `IngestionStage`에 `CRAWL` 추가 | enum 값 1개 additive 추가 + crawl 잡 기록 배선 | 양 레포 enum 동시 갱신/배포 필요(미갱신 읽기 측 `ValueError` 위험) | **✅ 적용됨 (2026-05-26)** — 양 레포 함께 배포 |
| 4. `soft_delete` 도입 | payload `is_deleted` 필드 + rag 검색 `must_not` 필터 + store soft_delete 메서드 | 양 레포 동시 배포, 기존 인덱스 백필/재색인 권장 | **✅ 적용됨 (2026-05-26)** — Sync Worker 호출 wiring은 운영 후속 |

항목 1·2·5는 문서/거버넌스 정합이며 런타임 변경이 없다. 항목 3·4는 사용자 승인 후 적용됐다(양 레포
동시 배포 필요). 항목 4의 삭제 트리거(Delta Sync/Trash/Webhook → `soft_delete_by_*`) 실배선은 운영
wiring 후속으로 남는다.

## 함께 수정한 문서

- 양 레포 `docs/db-schema.md` §1.4 ACL 절 — 미해결 → "PoC 결정(대안 A) — ADR 0003 참조".
- 양 레포 `docs/atlassian-api.md` ACL 미해결 절 — ADR 0003 결정 포인터 추가.
- 양 레포 `docs/ai/working-log.md` — 본 합의 세션 기록.
- 관련: ADR 0002(`space:{key}` prefix) — 항목 1이 이를 전제로 한다.
