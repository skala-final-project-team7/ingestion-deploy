# 0001. Page-level ACL 출처 — Confluence read restriction + Admin Key

- 상태: 채택 (api-spec v2.4/v2.5)
- 날짜: 2026-06-09
- 작성자: 최태성
- 적용 범위: `ingestion`(Data Ingestion Pipeline) · `rag`(RAG Pipeline) **양쪽 동일 기록**.
  본 ADR은 두 레포 `docs/adr/0001-*.md`에 동일 내용으로 복제한다.

> 본 ADR은 `docs/api-spec.md` §1-4(수집 자격증명·ACL)·§2-2 가 참조하는 **page-level ACL 적재 흐름**의
> 결정 기록이다. 공유 ACL 필드 계약(ingestion↔rag)의 owning ADR은 `0003`, prefix 규약은 `0002`이며,
> 본 ADR은 그 위에서 "ACL 출처(source)"를 page-level read restriction 으로 확정한 결정을 정리한다.

## 1. 배경

초기 PoC는 제공된 Atlassian 명세에 페이지 단위 권한 API가 없다고 보아 `["space:{space_key}"]` 합성으로
ACL을 만들었다(ADR 0002). 이후 사용자가 Atlassian **Admin Key**를 활성화하고 page-level **read
restriction API**(`GET /rest/api/content/{id}/restriction/byOperation/read`)로 페이지별 권한을 직접
수집할 수 있음이 실측으로 확인되었다(`docs/atlassian-api.md` "✓ ACL 결정"). 이에 ACL 출처를 space
단위 합성에서 페이지 단위 restriction 으로 격상한다.

## 2. 결정 — page-level read restriction 을 ACL 출처로 채택

### 2.1 ACL 적재 흐름 (수집 측)

1. Full Crawl 은 Admin Key 헤더(`Atl-Confluence-With-Admin-Key: true`)로 admin 이 접근 가능한
   **전체 스페이스**를 cross-space 수집한다(요청 `spaceKey` 없음 — api-spec v2.4).
2. 각 페이지에 대해 read restriction API 를 조회해 user/group restriction 을 `allowed_users`/
   `allowed_groups` 로 매핑한다(`ConfluenceRestrictionAclProvider`, `RAG_ATLASSIAN_USE_ADMIN_KEY=true`).
   group 식별자 우선순위·prefix 는 `RAG_ATLASSIAN_GROUP_ACL_FIELD_ORDER`/`RAG_ATLASSIAN_GROUP_ACL_PREFIX`
   로 제어한다.
3. restriction 이 비어 있는 페이지는 `RAG_ATLASSIAN_EMPTY_RESTRICTION_POLICY` 기본값
   `allow_authenticated` 에 따라 공개 sentinel `"*"`(`RAG_ATLASSIAN_PUBLIC_ACL_GROUP`)를 부여한다.
   보수적으로 운영하려면 `mark_missing`(빈 ACL → `INVALID_ACL` 색인 스킵)으로 전환한다.
4. 정책 적용 후에도 ACL 이 전혀 없으면 `INVALID_ACL` 로 색인하지 않는다.

### 2.2 ACL 적용 흐름 (검색 측)

- rag `app/query/acl.py:build_acl_filter` 가 요청 `userId`/`groups` 로 OR 매칭한다
  (`allowed_users` ∋ userId **OR** `allowed_groups` ∩ groups). 모든 principal 의 group 집합에 공개
  sentinel `"*"` 를 주입해 `allow_authenticated` 문서가 검색되게 한다. 검색 호출은 `@enforce_acl` 로
  강제되며 ACL 필터 없는 호출은 `ACLViolationError` 로 거부된다.

### 2.3 PoC 폴백

- `RAG_ATLASSIAN_USE_ADMIN_KEY=false`(기본) 또는 JSON fixture 경로에서는 ADR 0002 의
  `["space:{space_key}"]` 합성을 폴백으로 사용한다. 검색 seam(`build_acl_filter`)은 동일하므로 모델
  전환 시 색인만 재수행하면 된다.

## 3. 결과 / 영향

- credential 은 RabbitMQ·`/ml/ingest` payload 에 싣지 않는다. Data Ingestion Worker 는 `adminUserId`
  로 auth-server 내부 credential API(api-spec §2-5)를 조회해 admin OAuth `accessToken`+`cloudId` 를 얻는다.
- Admin Key 말소는 수집 완료/실패 후 RabbitMQ completion event → BFF consumer → auth-server
  deactivate 로 처리한다(ML 직접 말소 없음 — api-spec v2.5).
- 상위 folder/space 권한 상속 등 restriction 외 권한 결합은 후속 협의 대상이다(범위 밖).

## 4. 관련 문서

- `docs/api-spec.md` §1-4 · §2-2 · §2-5 (정본은 `rag` 레포)
- `docs/adr/0003-ingestion-rag-shared-contracts.md`(공유 ACL 필드 계약 owning ADR)
- `docs/adr/0002-acl-prefix-convention.md`(`space:{key}` prefix 규약 — 폴백)
- `docs/db-schema.md` §1.4 · `docs/atlassian-api.md` "✓ ACL 결정"
