# 0002. PoC ACL 그룹 prefix 컨벤션 — `space:{key}`

- 상태: **superseded (2026-06-11)**
- 날짜: 2026-05-17
- 작성자: 최태성

> **⛔ superseded (2026-06-11 회의 결정)**: ACL 값(`allowed_groups`)에 space key 를 싣는
> 모델은 **완전 제거**됐다 — "`allowed_groups` 필드에 포함되어 있던 스페이스 키는 현재
> 버전에서 사용하는 필드가 아니므로 코드에 남아있을 경우 제거" 합의 이행.
> `synthesize_space_acl`(atlassian 어댑터)·`space_fallback` 정책은 삭제됐고,
> JSON 픽스처 PoC 합성은 공개 sentinel `"*"` 부여로 대체됐다. 현행 모델:
> page-level read restriction 기반 `allowed_groups`(**groupId**)/`allowed_users`(accountId)
> + 빈 권한 정책(`mark_missing` 기본 / `allow_authenticated` opt-in) — `docs/api-spec.md`
> v2.6.x §2-1·§1-4, `docs/db-schema.md` §1.4, ADR 0003 참조. 아래 본문은 역사 기록이다.
>
> (구) 갱신 노트 (api-spec v2.4/v2.5, 2026-06-09): 운영 ACL 모델은 page-level read restriction
> 기반으로 진화했고 본 ADR 의 합성은 Admin-Key-OFF fallback 으로 유지된다 — **이 fallback 도
> 위 결정으로 제거됨.**

## 배경

`JsonFixtureSourceAdapter._synthesize_acl`(`app/adapters/json_fixture.py`)은 샘플 데이터에
ACL 필드가 없어 PoC 단계에서 `space_key` 기반으로 `allowed_groups`를 합성한다. 구현은
`[f"space:{space_key}"]` 형태(예: `["space:CLOUD"]`)를 채택했다. 그러나 이 prefix
컨벤션이 어디에도 동결되지 않았다.

검색이 동작하려면 **BFF가 발급하는 JWT의 `groups` 클레임**이 같은 prefix 컨벤션을 따라야
한다(예: JWT의 `groups=["space:CLOUD", "space:CCC"]`). 컨벤션이 어긋나면 모든 사용자
쿼리가 `RETRIEVAL_EMPTY`로 떨어진다(코드 리뷰 2026-05-17 P2,
working-log 2026-05-15 feature2 비고).

`examples/demo_search.py`에서도 이 컨벤션을 그대로 사용하며, 실 배포 시 BFF가 같은 형식을
보장해야 함이 시연으로 명확해졌다.

## 검토한 대안

### A. prefix 없이 raw space key (`["CLOUD"]`)

- 장점: 간결.
- 단점: 그룹과 스페이스 키의 네임스페이스가 충돌. 예를 들어 사용자 그룹명이 우연히 스페이스
  키와 같으면 의도하지 않은 권한 부여가 발생한다.

### B. `space:{key}` prefix (채택)

- 장점:
  - 그룹 네임스페이스 충돌 방지.
  - PoC에서 운영으로 전환할 때 BFF가 Confluence `DATA-03` 응답(사용자 접근 가능 스페이스
    목록)을 `space:` prefix를 붙여 JWT의 `groups` 클레임에 채우면 동일 코드가 동작한다.
  - `app/CLAUDE.md` §3 "ACL 필드 모델 교체 가능" 원칙과 정합 — `build_acl_filter`만
    교체하면 다른 prefix·다른 모델로 전환 가능.
- 단점: BFF가 이 컨벤션을 알아야 한다(본 ADR로 명시).

### C. content restrictions API로 페이지별 사용자·그룹 채집

- 설계서 원안. PoC API 명세에 없어 보류 — 운영 전환 시 재검토.

## 결정

대안 **B**를 채택한다.

- PoC ACL 그룹은 `space:{space_key}` 형식으로 표기한다.
- BFF는 JWT의 `groups` 클레임에 사용자가 접근 가능한 스페이스를 `space:{key}` 형식으로
  채운다. 예: 사용자가 CLOUD·CCC 스페이스 접근 가능 → `groups=["space:CLOUD","space:CCC"]`.
- 이 컨벤션은 `JsonFixtureSourceAdapter._synthesize_acl`과 `examples/demo_search.py`가
  암묵적으로 따르던 것을 명시적으로 동결한 것이다.
- `AtlassianSourceAdapter` 구현 시(feature2 잔여, current-plan) `DATA-03` 응답 매핑이 이
  컨벤션을 따른다.
- 향후 content restrictions API 도입으로 페이지별 `allowed_users`/`allowed_groups`를 채울
  경우, `build_acl_filter`만 교체하고 `space:` prefix는 그대로 유지하거나 별도 ADR로
  변경한다.

## 영향

- 수정 파일: 없음(기존 구현이 이미 `space:` prefix를 사용함 — 본 ADR은 그것을 동결).
- 후속 작업:
  - `AtlassianSourceAdapter` 구현 시 `DATA-03` 응답 매핑에서 `space:` prefix 부착.
  - BFF 담당자 협의 — JWT `groups` 클레임 형식 동결. `docs/api-spec.md`의 JWT 클레임
    예시도 `groups=["space:..."]`로 갱신할지 결정.
- 다른 담당자 영역: BFF/Authorization Server 담당자가 본 ADR을 인지해야 한다.

## 함께 수정한 문서

- `docs/ai/working-log.md` — 결정 사항 기록.
- 본 ADR이 채택되면 `docs/api-spec.md`의 JWT 클레임 예시도 갱신 권장(별도 PR).
