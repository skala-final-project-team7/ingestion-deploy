# DB Schema

이 문서는 RAG 파이프라인이 사용하는 데이터 저장소 스키마를 정의한다.
RAG 파이프라인 설계서 v0.2.2(`docs/rag-pipeline-design.md`)·청킹 전략 설계서(`docs/chunking-strategy.md`)와
정합한다. 스키마 변경 시 이 문서를 함께 수정한다(루트 `CLAUDE.md` 규칙).

저장소 구성: **Qdrant**(벡터 검색) · **MongoDB**(문서·잡·임베딩 캐시) · **MySQL**(스페이스 doc_type 캐시).
사용자·대화·피드백 등 정형 데이터는 백엔드 담당 영역이다.

> 본 PoC에서 일부 컬렉션은 백엔드 어댑터가 적재한 mock(`rag_mock.*`)을 읽기 전용으로 사용한다.

---

## 1. Qdrant — Multi-Pool Vector Store

청크를 정보 특성에 따라 3개 Collection(Pool)으로 분리 저장한다. 세 Pool은 동일한 Named Vector
구조와 Payload 스키마를 가지며 컬렉션명만 다르다.

| Collection | 임베딩 대상 텍스트 | 검색 특성 |
|---|---|---|
| `title_pool` | `page_title` + `section_header` (첨부: `attachment_filename` + `section_header`) | 제목/섹션명 정확 매칭 |
| `content_pool` | 청크 본문 텍스트 | 의미 유사도 기반 본문 검색 |
| `label_pool` | `labels` + `space_key` + `doc_type`/`attachment_type` 결합 짧은 텍스트 | 카테고리/태그 부스팅 |

### 1.1 Vector 구성

```jsonc
{
  "vectors":        { "dense": { "size": 1024, "distance": "Cosine" } },
  "sparse_vectors": { "sparse-bm25": { "modifier": "idf" } },
  "shard_number": 2,
  "replication_factor": 1,
  "on_disk_payload": true
}
```

- `dense` — `intfloat/multilingual-e5-large`, 1024차원 (1차 후보, PoC 2주차 벤치마크로 확정)
- `sparse-bm25` — BM25 Sparse 벡터 (KoNLPy Mecab / Kiwi 토크나이저)

### 1.2 Payload 스키마 (모든 Point 공통)

| 필드 | 타입 | 설명 |
|---|---|---|
| `chunk_id` | string | 결정론적 청크 식별자 (`SHA1(page_id + chunk_index + attachment_id)`, 40자 hex). Point ID 매핑의 원본·검색 결과 복원 키 — 아래 Point ID 매핑 참조 |
| `page_id` | string | 문서 단위 삭제/갱신 키. 첨부 청크도 부모 page_id 보존 |
| `page_title` | string | 출처 카드 제목 |
| `section_header` | string | 섹션명 (본문 H2/H3, 첨부 `p.<N>` 또는 시트명). 빈 문자열 금지 |
| `section_path` | string | `ancestors` + `section_header` 결합 계층 경로 |
| `chunk_index` | integer | 동일 페이지/첨부 내 0-based 순서 |
| `labels` | string[] | lowercase·하이픈 정규화 |
| `doc_type` | string | 본문 6유형 중 하나 / 첨부는 `attachment_type` 값 |
| `space_key` | string | ACL 1차 키 + 출처 카드 표기 |
| `space_id` | string | 출처 카드 `sources[].spaceId` 원천 (2026-06-10 A8 후속 — 구버전 payload 는 부재 → 빈 문자열 복원) |
| `space_name` | string | 출처 카드 `sources[].spaceName` 원천 (동상) |
| `allowed_groups` | string[] | **ACL 필터 (필수)** |
| `allowed_users` | string[] | **ACL 필터 (필수)** |
| `webui_link` | string | Confluence 원본 URL (가능 시 `#anchor` 포함) |
| `last_modified` | datetime | 출처 '수정일' + Delta Sync 비교 키 |
| `version_number` | integer | 재색인 시 멱등성 검사 |
| `is_deleted` | boolean | soft-delete 플래그 (**ADR 0003 항목 4**). 신규/재색인 upsert 는 `false`. 삭제 확정 시 `store.soft_delete_by_*` 가 `true` 로 set_payload — 트리거는 `SyncWorker` 가 Delta 확인 게이트·Confluence Trash API·실시간 Webhook 을 `apply_soft_deletes` 로 배선(**featureI-5b**). rag 검색이 `is_deleted=true` 를 `must_not` 으로 제외. 필드 부재(legacy)는 미삭제로 간주 |
| `source_type` | string | `page` \| `attachment` |
| `attachment_id` | string | 첨부 단위 삭제·갱신 식별 (본문 청크는 null) |
| `attachment_filename` | string | 출처 카드 첨부 파일명 |
| `attachment_mime` | string | UI 아이콘 분기 |
| `extracted_format` | string | `raw_text` \| `sheet_serialized` |
| `token_count` | integer | 청커가 산출한 토큰 수 (`ChunkMetadata.token_count`). 검색 결과 재구성(`app/query/search_node._chunk_from_search_hit`) 시 그대로 복원해 답변 생성기/검증 단계가 동일 메타데이터를 본다 |
| `text` | string | 청크 풀 텍스트 (feature17c-7). 재순위화(Cross-Encoder)·답변 생성기가 200자 프리뷰가 아닌 풀 텍스트로 동작하도록 payload에 동봉한다. 검색 결과 재구성(`_chunk_from_search_hit`)이 이 필드로 `chunk.text`를 복원한다. legacy 인덱스에 없으면 `text_preview`로 fallback. 풀 텍스트가 3 Pool에 중복 저장되므로 대규모 운영에서는 메모리 트레이드오프가 있다(데모/평가 코퍼스에서는 무시 가능; 대규모 전환 시 `chunk_lookup` 조회 방식으로 이관 고려) |
| `text_preview` | string | 청크 본문 첫 200자 — UI 출처 카드 미리보기용 |

**Point ID 매핑.** Qdrant Point ID는 unsigned int 또는 UUID 형식만 허용하므로 SHA1 hex
`chunk_id`(40자)를 Point ID로 직접 사용할 수 없다. RAG 어댑터
(`app/storage/qdrant_client.py`)는 `uuid5(uuid.NAMESPACE_OID, chunk_id)` 로 결정론
UUID를 생성하여 Point ID로 사용한다. 동일 `chunk_id` → 동일 UUID → Qdrant 레벨에서도
멱등 upsert가 성립한다 (`app/CLAUDE.md` §4). 검색 결과에서 원본 `chunk_id` 는 위
payload 필드로 복원한다.

### 1.3 Payload 인덱스 (필터 성능)

`keyword`: `chunk_id`, `allowed_groups`, `allowed_users`, `space_key`, `labels`,
`doc_type`, `page_id`, `attachment_id`, `source_type` / `datetime`: `last_modified` /
`bool`: `is_deleted` (ADR 0003 항목 4 — soft-delete `must_not` 제외 필터 성능)

### 1.4 ACL 강제 적용

검색 시 ACL 필터는 `@enforce_acl` 데코레이터에서 항상 `AND`로 주입된다. ACL 조건이 빠진 검색
호출은 `ACLViolationError`로 거부된다. 상세는 `docs/rag-pipeline-design.md` §6.

> **✓ ACL 필드 모델 (api-spec v2.4/v2.5) — page-level `allowed_groups`/`allowed_users` 채택,
> `space_key` 합성은 Admin-Key-OFF 폴백** (ingestion↔rag 합의, **ADR 0003** 참조).
> 설계서·기획서 §6.6은 ACL을 청크별 `allowed_groups`/`allowed_users` Payload로 정의한다. 초기에는
> 명세에 페이지 단위 권한 API가 없다고 보아 Space 단위 합성을 PoC 로 썼으나, 이후 Admin Key +
> page-level read restriction(`/rest/api/content/{id}/restriction/byOperation/read`)으로 페이지별
> 권한 수집이 가능함이 확인되어 아래 **(B) page-level 을 채택**하고 (A) `space_key` 합성은 Admin Key
> 미사용 시 폴백으로 둔다.
>
> - **(A) `space_key` 기반 — PoC 폴백(admin key off).** `atlassian_use_admin_key=False`이면 수집 시
>   `allowed_groups`를 `["space:{space_key}"]`로 합성하고(`_synthesize_acl`/`synthesize_space_acl`),
>   검색 시 `app/query/acl.py:build_acl_filter`가 JWT `groups`(`space:{key}` 형식 — ADR 0002)를
>   `allowed_groups`에 OR 매칭한다. 입도는 스페이스 단위.
> - **(B) `allowed_groups`/`allowed_users`(페이지별) — 채택(admin key on).** `atlassian_use_admin_key=True`
>   이면 Admin Key 로 `/rest/api/content/{pageId}/restriction/byOperation/read` 를 조회해
>   page-level read restriction 을 `allowed_groups`/`allowed_users`로 매핑한다
>   (`ConfluenceRestrictionAclProvider`). Full Crawl 은 Admin Key 로 접근 가능한 **전체 스페이스**를
>   수집하므로 space key 입력은 불필요하다. group 식별자 우선순위/prefix 는
>   `atlassian_group_acl_field_order` / `atlassian_group_acl_prefix` 로 제어한다.
>
> **빈 restriction 처리 (`atlassian_empty_restriction_policy`)** — page-level restriction 이 비어 있는
> 페이지(=조직 내 인증 사용자 누구나 열람 가능)는 다음 정책으로 분기한다:
> - `allow_authenticated`(opt-in — 기본은 `mark_missing`(fail-closed), 2026-06-10 코드 리뷰 A2): `allowed_groups`에 sentinel group(`atlassian_public_acl_group`, 기본
>   `"*"`)을 부여해 **모든 인증 사용자**에게 허용한다. **공유 계약** — 이 sentinel 이 실제 검색 허용으로
>   이어지려면 RAG `app/query/acl.py:build_acl_filter` 가 동일 토큰을 모든 principal 의 `groups`에
>   주입해야 한다(ingestion↔rag, **ADR 0003**). 미주입 시 색인은 되지만 검색에 노출되지 않는다.
> - `space_fallback`: `["space:{space_key}"]` 합성(공간 단위 폴백).
> - `mark_missing`: 빈 ACL 유지 → 색인 단계 `INVALID_ACL` 로 차단(보수 정책).
>
> 모델 교체 여지 보존을 위해 Payload는 `space_key` + `allowed_groups` + `allowed_users`를 **모두
> 인덱싱**한 채로 둔다. 검색 필터 생성은 `app/query/acl.py`에 격리돼 결정에 따라 그 함수만 교체한다.

---

## 2. MongoDB

### 2.1 `rag_mock.pages` (PoC, 읽기 전용 — 백엔드 적재)

표준 `PageObject` 형태. 필드는 `docs/rag-pipeline-design.md` §7.1 참조
(`page_id`, `space_key`, `title`, `body_html`, `labels[]`, `ancestors[]`, `version_number`,
`last_modified`, `allowed_groups[]`, `allowed_users[]`, `webui_link`, `attachments[]`).

### 2.2 `rag_mock.attachments` (PoC, 읽기 전용 — 백엔드 적재)

`attachment_id`, `filename`, `mime_type`, `extracted_text`, `extracted_format`,
`file_size_bytes`, `download_url`, `parent_page_id`, `last_modified`.

### 2.3 `ingestion_jobs` (RAG 파이프라인 기록)

| 필드 | 타입 | 설명 |
|---|---|---|
| `page_id` | string | 대상 페이지 |
| `attachment_id` | string \| null | 대상 첨부 (본문 잡은 null) |
| `stage` | string | `crawl` / `analyze` / `chunk` / `embed` / `upsert` / `sync` (`IngestionStage` enum). `crawl` 은 수집 단계로 **ADR 0003 항목 3**으로 추가됨(ingestion↔rag 공유 enum, 양 레포 동시 갱신). ingestion `crawler.run_full_crawl` 가 `jobs` 주입 시 페이지별 CRAWL SUCCESS 를 기록한다 |
| `status` | string | 정상 또는 예외 코드 (`PARTIAL_PARSE`, `INVALID_ACL`, `ATTACH_ENCRYPTED`, `UNSUPPORTED_ATTACH_TYPE`, `LOW_QUALITY_ATTACH`, `ATTACH_NO_HEADER`, `OVERSIZE_ATOMIC`, `TOKENIZER_FAIL` 등 — `docs/chunking-strategy.md` §8) |
| `started_at` / `finished_at` | datetime | 처리 구간 |
| `error` | string \| null | 실패 상세 |

**적재 흐름.** `app/storage/jobs.py` 의 `IngestionJobsRepository.record` /
`record_many` 가 Ingestion 그래프 각 노드 종료 시점에 7필드 레코드를 적재한다.
`stage`/`status` 는 `IngestionStage` / `IngestionStatus` enum 의 `.value` 문자열로
직렬화. `record_many` 는 빈 입력에서 short-circuit 해 pymongo `insert_many` 의
`InvalidOperation` 을 회피한다. 관리자 대시보드 조회 API 는 별도 시스템 책임이므로
본 어댑터는 적재만 노출 (오버튜닝 회피).

**인덱스 권장.** 운영에서는 관리자 대시보드 조회 패턴에 맞춰 `(page_id, started_at)`
복합 인덱스 + `status` 단일 인덱스를 권장 (실패 잡 필터링). 본 milestone 은 적재
어댑터만 추가, 인덱스 생성은 운영 부트스트랩 단계에서 별도 처리.

### 2.4 `embedding_cache` (멱등성)

`chunk_id`, `version_number`, `dense_hash`, `sparse_hash`, `computed_at`.
동일 `chunk_id` + `version_number`는 재임베딩·재upsert 스킵.

### 2.5 `chunk_lookup` (청크 풀 텍스트·첨부 download_url)

청크 단위 풀 텍스트와 첨부 다운로드 URL을 `chunk_id` 키로 조회하는 컬렉션. Qdrant
payload의 `text_preview`(첫 200자) 한계를 보완하고, `Source.download_url`에 첨부
청크의 사용자 노출용 URL을 채우기 위해 사용한다 (`app/storage/chunk_lookup.py`).

| 필드 | 타입 | 설명 |
|---|---|---|
| `chunk_id` | string (PK / unique index) | 결정론적 청크 식별자 (40자 SHA1 hex) |
| `text` | string | 청크 풀 텍스트. 답변 생성기·검증기가 200자 한계를 넘는 컨텍스트가 필요할 때 조회 |
| `download_url` | string \| null | 첨부 청크일 때만 채워지는 사용자 노출용 URL. 본문 청크는 null |
| `updated_at` | datetime | 적재·갱신 시각 |

**인덱스.** `chunk_id` unique 인덱스 1개로 O(1) 룩업.

**적재 흐름.** `app/ingestion/indexer.py`의 `index_chunks`가 모든 Pool upsert + cache
write 성공 직후 `ChunkTextLookup.upsert_many` 로 단일 배치 적재한다 (Phase 4). cache hit
으로 스킵된 청크는 적재 대상에서 제외 — `embedding_cache` 와 멱등성 정합. 본문 청크는
`download_url=null`, 첨부 청크는 호출자가 주입한 `attachment_download_urls` 매핑
(`attachment_id -> download_url`)에서 조회해 채운다. `updated_at` 은 `MongoChunkTextLookup`
어댑터가 적재 시점(UTC)에 자동 부여한다. cache write 이후 단계라 chunk_lookup 적재 실패가
멱등성 캐시 상태를 오염시키지 않는다.

### 2.6 `raw_pages` (Data Ingestion Agent 수집 원본 — FR-001)

Data Ingestion Agent(Full Crawl) / Data Sync Agent(Delta) 가 수집한 표준 `PageObject` 를
적재한다. 후속 Chunking Worker 가 `page_id` 로 원본 본문을 조회한다. 적재 어댑터는
`app/storage/raw_store.py` 의 `RawPageStore`(ABC) / `FakeRawPageStore` / `MongoRawPageStore`.

| 필드 | 타입 | 설명 |
|---|---|---|
| `page_id` | string (upsert 키) | 문서 식별자. 같은 page_id 재크롤 시 멱등 upsert |
| `space_key` | string | ACL 1차 키 + 출처 |
| `title` | string | 페이지 제목 |
| `body_html` | string | Confluence storage HTML 원문(청커가 파싱) |
| `version_number` | integer | 재색인 멱등성 비교 키 |
| `last_modified` | datetime(ISO) | Delta Sync 변경 비교 키 |
| `allowed_groups` / `allowed_users` | string[] | 운영 ACL(page-level read restriction; 빈 권한은 `"*"` sentinel). Admin-Key-OFF는 `space:{key}` 합성 폴백 — §1.4 |
| `webui_link` | string | 출처 카드 원본 URL |
| `labels` / `ancestors` / `attachments` | array | 에이전트 MVP 미산출 → 현재 빈 배열(TBD) |

> 적재 본문은 `PageObject.model_dump(mode="json")`. 멱등 upsert(`page_id` 필터)로 재크롤·
> Delta 재투입이 안전하다. 본문·자격증명은 큐 메시지에 싣지 않고 `page_id` 만 전달한다.

### 2.7 `raw_attachments` (첨부 원본 — FR-001/FR-002)

페이지 첨부의 메타·텍스트·다운로드 핸들을 `attachment_id` 키로 멱등 적재한다
(`MongoRawPageStore.save_attachment`). 표준 `Attachment`(설계서 §3.2)를 그대로 적재하며,
첨부 Chunking Worker 가 `attachment_id` 로 조회(`get_attachment`)해 청킹한다.

| 필드 | 타입 | 설명 |
|---|---|---|
| `attachment_id` | string (upsert 키) | 첨부 식별자. 같은 id 재수집 시 멱등 upsert |
| `filename` | string | 첨부 파일명(출처 카드·유형 추정) |
| `mime_type` | string | MIME 타입(유형 분류 1차 키) |
| `extracted_text` | string | 추출 텍스트. `analyze_attachment` 의 유효성 게이트(길이·반복비율) 입력 |
| `extracted_format` | string | `RAW_TEXT` / `SHEET_SERIALIZED` (`ExtractedFormat`) |
| `download_url` | string | 사용자 노출용 URL — `chunk_lookup.download_url` 로 전달 |
| `parent_page_id` | string | 부모 페이지(ACL·메타 상속원) |
| `last_modified` | datetime(ISO) | Delta Sync 변경 비교 키 |
| `file_size_bytes` | integer \| null | 첨부 크기(optional) |
| `local_path` | string \| null | 청커가 파일을 직접 열 로컬 경로(ADR-2026-001). 운영 어댑터가 다운로드 시 채움 |

> **적재 흐름(featureI-3b).** `crawler.run_full_crawl` 이 `page.attachments` 가 있으면 첨부를
> `raw_attachments` 에 적재하고 첨부 `content.chunking`(`source_type=attachment`) 메시지를
> 발행한다. Worker(`_process_attachment_message`)는 부모 페이지 ACL 상속 게이트 →
> `analyze_attachment` → `chunk_attachment`(파일 직접 읽기) → `index_chunks` 로 색인한다.
> 청킹은 rag 레포와 동일하게 파일 기반이므로 별도 `attachment_texts` 컬렉션은 두지 않는다
> (`extracted_text` 는 본 컬렉션에 함께 보존). **남은 것은 실 Confluence 첨부 다운로드 어댑터**
> (바이너리 수집 → `extracted_text`/`local_path` 채움)로, 인프라 의존이라 후속이다. vendored
> 에이전트 MVP 는 첨부를 수집하지 않아(`attachments=[]`) 운영 경로에서는 아직 비어 있다.

---

## 3. MySQL

### 3.1 `space_doc_type_cache` (문서 분석기 Agent 결과 캐싱)

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `space_key` | varchar (PK) | Confluence 스페이스 식별자 |
| `dominant_doc_type` | varchar | 지배적 문서 유형 |
| `secondary_doc_types` | json | 보조 유형 목록 |
| `confidence` | decimal | 판별 신뢰도 (< 0.6 시 `operation` fallback) |
| `analyzed_at` | datetime | 분석 시각 |
| `sample_count` | int | 분석에 사용한 샘플 페이지 수 |

스페이스 단위 1회 LLM 호출 결과를 캐싱하여 이후 모든 문서에 재사용한다.

---

## 4. 변경 규칙

- Qdrant Collection·Payload·인덱스, MongoDB 컬렉션, MySQL 테이블 변경 시 이 문서를 함께 수정한다.
- 임베딩 모델/차원 변경은 `*_pool` Collection 재생성을 동반하므로 Plan에 영향 범위를 명시한다.
- ACL 관련 필드(`allowed_groups`, `allowed_users`, `source_type`)는 권한 필터링 정확도와 직결되므로 임의로 제거하지 않는다.
- `chunk_id` 생성 규칙(결정론적 SHA1)은 멱등 upsert의 전제이므로 변경 시 전체 재색인이 필요하다.
