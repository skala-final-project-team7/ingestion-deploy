# Atlassian / Confluence API (구현 참조)

이 문서는 `Atlassian API 명세서`를 RAG 파이프라인 구현 관점에서 정리한 참조 문서다.
백엔드(BFF)가 아직 구축되지 않아, PoC에서는 **ML 파이프라인(본 저장소)이 Atlassian REST API를
`atlassian-python-api` 라이브러리로 직접 호출**한다. OAuth 인증·토큰 관리는 Authorization Server
(Spring Security OAuth2 Client)가 담당하며, 본 저장소는 발급된 `access_token`을 전달받아 사용한다.

---

## 책임 경계

| 영역 | 담당 | API |
|---|---|---|
| 인증 (OAuth 2.0 3LO) | Authorization Server (Spring) | `AUTH-01~04` — 로그인 리다이렉트 / 토큰 교환 / 토큰 갱신 / accessible-resources |
| 데이터 수집 | **ML 파이프라인 (본 저장소)** | `DATA-01~03` — 페이지 목록 / CQL 검색 / Space 목록 |

본 저장소는 `access_token` + `cloudid`를 입력으로 받아 데이터 수집 API를 호출한다.
모든 데이터 수집 요청에 `Authorization: Bearer {access_token}` 헤더가 필요하다.

> **⚠ admin-key 경로 예외 (api-spec v2.6.1, 2026-06-11 정정)**: page-level read restriction 우회용
> **Admin Key 가 적용되는 호출은 OAuth Bearer 로 불가**하다(Confluence admin-key REST 리소스는
> Forge/OAuth2/Connect 앱 접근 불가 — 공식). 해당 호출은 `Authorization: Basic
> base64(adminEmail:adminApiToken)` + `Atl-Confluence-With-Admin-Key: true` 헤더로 **site URL**
> (`https://{site}.atlassian.net/wiki/...`)에서 수행해야 한다 — 아래 OAuth 게이트웨이 URL
> (`api.atlassian.com/ex/confluence/{cloudid}`) 경로에서는 admin-key 가 동작하지 않으며, 위반 시
> restricted 페이지가 무음 누락된다. credential 모델 상세는 rag 레포 `docs/api-spec.md` §1-4·§2-5 참조.
>
> **하이브리드 확정 (api-spec v2.6.2 — BE 2026-06-11)**: 위 "OAuth 불가" 제약의 **확정 적용 범위는
> admin-key 관리(activate/deactivate) 호출**이며 auth-server 내부 전용이다(ML 은 admin API Token
> 비취급). 콘텐츠 조회의 계약은 OAuth Bearer + 게이트웨이 + Admin Key 헤더이나 **3단계 검증
> 게이트**로 남아 있다 — ML 실측은 Basic + site URL 조합만 확인됐으므로 본 레포는
> `RAG_ATLASSIAN_USE_ADMIN_KEY` 토글로 검증된 경로를 보존한다. §2-5 응답은
> `{accessToken, cloudId, siteUrl, expiresAt}` — **`siteUrl`(=`admin_atlassian_credential.site_url`
> 단일 컬럼)은 `_links.webui` 상대경로의 absolute 정규화에 사용**한다(아래 매핑표).

## 데이터 수집 API

API URL 형식: `https://api.atlassian.com/ex/confluence/{cloudid}/rest/api/...`
(`cloudid`는 `AUTH-04 accessible-resources` 응답의 `id`)

### DATA-01. 페이지 목록 조회 (Full Crawl)

`GET /content?type=page&spaceKey={key}&start={n}&limit={≤100}&expand=space,version,body.storage`

서비스 최초 구동 시 전체 문서를 Vector DB에 적재하기 위해 사용. `limit` 최대 100,
초과 시 `start`를 증가시키며 반복 호출(또는 `get_all_pages_from_space_as_generator()`).

### DATA-02. CQL 검색 (Delta Sync)

`GET /content/search?cql={query}&limit={≤100}&expand=body.storage,version,space`

1시간 주기 델타 싱크에 사용. 예: `space="ENG" AND type=page AND lastModified >= "2026-04-29 00:00"`.
`_links.next`가 있으면 커서 기반 다음 페이지 존재.

### DATA-03. Space 목록 조회 (사용자 권한 필터링)

`GET /space?start={n}&limit={≤500}`

로그인 사용자가 **접근 가능한 Space만** 반환된다(Confluence 권한 자동 적용).
**cross-space 크롤의 스페이스 열거**에 사용한다. ACL 필터 자체는 페이지 단위 read restriction 으로
적재한다(아래 "✓ ACL 결정" 참조).

## 페이지 객체 → PageObject 매핑

`DATA-01/02` 응답의 페이지 객체는 `samples/confluence_sample_data.json`과 동일한 형식이다.

| Atlassian 필드 | PageObject 필드 | 비고 |
|---|---|---|
| `id` | `page_id` | Qdrant 문서 식별자 |
| `title` | `title` | |
| `body.storage.value` | `body_html` | HTML — BeautifulSoup 파싱 필요 |
| `version.number` | `version_number` | 멱등성 검사 |
| `version.when` | `last_modified` | ISO 8601 |
| `space.key` | `space_key` | |
| `metadata.labels.results[].name` | `labels[]` | |
| `ancestors[].{id,title}` | `ancestors[]` | |
| `_links.webui` | `webui_link` | 출처 카드 원본 링크 — **상대경로를 §2-5 `siteUrl`(=`RAG_ATLASSIAN_SITE_URL`) 기준 absolute 로 정규화해 적재**(`normalize_webui_link`, 2026-06-11). 미주입 시 상대경로 passthrough + WARNING |
| `attachments[]` | `attachments[]` | 샘플 데이터에 메타만 존재 — 실제 다운로드/추출은 별도 |
| read restriction | `allowed_groups[]` / `allowed_users[]` | page-level read restriction 에서 산출(Admin Key); 빈 권한은 공개 sentinel `"*"` — 아래 "✓ ACL 결정" 참조 |

## ✓ ACL 결정 (api-spec v2.4/v2.5 — page-level 채택)

설계서·기획서 §6.6은 ACL을 청크별 `allowed_groups`/`allowed_users` Payload로 정의한다. 초기에는
명세에 페이지 단위 권한 API가 없다고 보아 space 기반 합성을 PoC 로 썼으나, 이후 Atlassian **Admin Key
+ page-level read restriction API**(`GET /rest/api/content/{id}/restriction/byOperation/read`)로
페이지별 권한을 수집할 수 있음이 확인되었다.

→ **결정됨(api-spec v2.4/v2.5, `docs/adr/0003-ingestion-rag-shared-contracts.md` 항목 1):**
운영 ACL은 페이지 단위 `allowed_groups`/`allowed_users`를 **채택**한다. 수집 시 Admin Key 헤더
(`Atl-Confluence-With-Admin-Key: true`)로 read restriction 을 조회해 산출하고
(`ConfluenceRestrictionAclProvider`, `RAG_ATLASSIAN_USE_ADMIN_KEY=true`), restriction 이 빈 페이지는
`allow_authenticated` 정책의 공개 sentinel `"*"`(`RAG_ATLASSIAN_PUBLIC_ACL_GROUP`)로 적재한다. 검색 시
`app/query/acl.py:build_acl_filter`가 `userId`/`groups`에 OR 매칭하며 `"*"`를 항상 주입한다.
**space key 합성 fallback 은 2026-06-11 회의 결정으로 제거됐다**(ACL 값의 space key 레거시 폐기 —
ADR 0002 superseded): Admin Key 미사용(`RAG_ATLASSIAN_USE_ADMIN_KEY=false`) 경로는 빈 ACL
(fail-closed — 색인 단계 `INVALID_ACL` 제외)이며, PoC fixture 는 공개 sentinel `"*"` 를 부여한다.
`docs/db-schema.md` §1.4·`docs/ai/current-plan.md` 참조.

## Cloud ↔ Server/DC 전환

URL 3개(인증 요청 / 토큰 교환 / API 호출)만 환경 변수로 관리하면 코드 수정 없이 전환 가능
(`app/config.py`에서 관리).

## 공식 문서

- OAuth 2.0 3LO: https://developer.atlassian.com/cloud/confluence/oauth-2-3lo-apps/
- REST API v1: https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-content/
- CQL: https://developer.atlassian.com/cloud/confluence/advanced-searching-using-cql/
