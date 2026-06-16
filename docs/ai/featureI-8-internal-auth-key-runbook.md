# featureI-8 Internal credential lookup 운영 점검 가이드

## 목적

Data-Ingestion Worker가 `GET /internal/auth/admin-confluence-credential` 호출에서
`X-Internal-Api-Key`로 auth-server를 조회할 때, 키 미합의/누락으로 인한 401 오류를
사전 탐지하고 운영에서 빠르게 대응할 수 있도록 점검/알림 기준을 정한다.

## 1. 배포 전 체크리스트

### 공통(스테이징/운영)

- `RAG_INTERNAL_AUTH_SERVER_BASE_URL` 값이 배포 대상 환경의 auth-server 엔드포인트를 가리키는지 확인
- `RAG_INTERNAL_API_KEY` 값이 환경별 Secret에서 주입되는지 확인
- 배포 환경에서 `env`/`config`에 `INTERNAL_API_KEY`(운영 키명)와 동일한 값인지 확인
- Secret 이름/네임스페이스/권한이 동일한지 확인
- `INTERNAL_API_KEY`가 문자열 비어있음이 아닌지 확인(빌드/런타임 값 노출 없는 상태)
- `auth-server` 쪽 `lina.internal.api-key`(또는 동등 환경변수)와 값 일치 여부 검증

### 스테이징

- `INTERNAL_API_KEY`가 존재하지 않으면 Data Ingestion Worker가 startup 경고를 남기는지 확인
- internal 호출에만 `X-Internal-Api-Key`가 붙고 외부 호출에는 붙지 않는지 테스트(기본 health / webhook 호출)
- `/internal/auth/admin-confluence-credential` 200/400/401/403/404 경로 각각 수동 검증

### 운영

- 배포 직후 10분 단위로 `adminUserId` 조회 성공률/401/403/404/5xx를 확인
- `auth-server`와 Data Ingestion의 Secret 동기화 타임라인(회전/롤링) 문서화

## 2. 운영 알림 규칙(권장)

- 경고 이벤트 예시: `admin credential lookup 401` 로그
- `INTERNAL_API_KEY` 누락 추정(메시지에 `누락` 포함) 또는 미스매치 추정(`미스매치` 포함) 발생 시
  - 동일 시간대 동일 메시지 2회 이상이면 `InternalAuthKeyMismatch` 경보 발생
  - 1시간 창에서 `INTERNAL_API_KEY` 관련 경보가 누적 2건이면
    PagerDuty/Slack 알림 대상자에게 즉시 통보
- 403/404은 권한/로그인 상태 점검 알림 라우팅으로 분기
- 5xx/네트워크 계열은 auth-server 장애 또는 자격증명 계약 이슈로 판단, 재시도/네트워크 회복 상태 모니터링

## 3. 현장 대응 절차(간단)

1. 401 로그에서 `adminUserId`와 `requestedAt`를 기준으로 요청 배치(job)를 조회
2. Secret 일치 여부 확인 (`RAG_INTERNAL_API_KEY` vs `lina.internal.api-key`)
3. 불일치면:
   - 배포 직후 Secret 주입 템플릿/Helm 값 동기화 상태를 롤백 또는 재적용
   - key 회전 정책이 있는 경우 `auth-server`와 `ingestion-worker` 동시 적용 여부 확인
4. 누락으로 판단되면:
   - 해당 환경 변수 설정 누락 여부 수정
   - 수동 재배포

## 4. 변경 협의 항목(잔여)

- 내부 키 값은 기본적으로 auth-server에서 생성 후 Worker로 공유
- Secret 전파 채널(배포 파이프라인/Helm values/CI Vault)을 통해 키 회전 자동화
- NetworkPolicy 단독 우회는 기본 정책으로 미채택(현재는 헤더 필수 정책 유지)
