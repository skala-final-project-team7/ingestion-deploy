# RabbitMQ completion event 구현 지침서

본 지침서는 Data Ingestion Worker가 BFF와 연동되는 completion event 발행 계약을 정리한다.

## 1) MQ 계약 확정

- 현재 BFF consumer는 queue 이름 기준 consume.
- completion queue: `lina.admin.ingest.completion`
- DLQ: `lina.admin.ingest.completion.dlq`
- BFF consumer: `@RabbitListener(queues = "${lina.admin.ingest.rabbitmq.completion-queue}")`
- Data Ingestion Worker 기본 publish:
  - exchange: `""` (default exchange)
  - routingKey: `lina.admin.ingest.completion`
  - deliveryMode: `PERSISTENT`
- `named exchange`를 꼭 써야 할 경우:
  - BFF에 completion exchange/routing-key/binding 설정을 추가해야 함.
  - 현 구현 기준은 default exchange + queue 이름 routing이 정합함.

## 2) completion event DTO

### 이벤트 스키마

```json
{
  "jobId": "job-...",
  "adminUserId": "admin-account-id",
  "mode": "full",
  "status": "COMPLETED",
  "completedAt": "2026-06-11T08:00:00Z",
  "errorCode": null,
  "message": "done"
}
```

### 필수 필드

- `jobId`
- `adminUserId`
- `mode`
- `status`
- `completedAt`

### 허용 status

- `COMPLETED`
- `FAILED`

### 금지 필드 (payload에 포함하면 안 됨)

- `accessToken`
- `refreshToken`
- `cloudId`
- `adminApiToken`
- `adminEmail`

## 3) 발행 시점

Worker 흐름:

1. ingest job consume
2. auth-server에서 admin credential 조회
3. Confluence 수집 수행
4. 성공 시 `COMPLETED` event publish
5. 실패 시 `FAILED` event publish
6. completion event publish 실패 시 worker의 로그/재시도 정책 적용

중요: 수집 실패라도 반드시 `FAILED`를 발행해야 BFF 쪽 Admin Key deactivate 경로가 항상 동작한다.

## 4) 메시지 영속성

- publish 시:
  - `deliveryMode = PERSISTENT`
  - `contentType = application/json`
- 가능하면 publisher confirm을 활성화해 broker 반영 여부 확인.

## 5) 실패 처리 정책

수집 중 예외 발생 시:

- exception catch
- errorCode 생성
- message는 민감정보 없이 요약
- `FAILED` completion event publish

예시:

```json
{
  "jobId": "job-1",
  "adminUserId": "admin-account-id",
  "mode": "full",
  "status": "FAILED",
  "completedAt": "2026-06-11T08:00:00Z",
  "errorCode": "CONFLUENCE_FETCH_FAILED",
  "message": "Confluence page fetch failed"
}
```

주의: token, email, cloudId, URL query secret 등을 message에 넣지 않음.

## 6) 테스트 항목

- 성공 수집 완료 시 `COMPLETED` event publish
- 수집 실패 시 `FAILED` event publish
- payload에 credential 금지 필드 미포함
- `jobId`/`adminUserId` 누락 시 publish 미실행 또는 명시적 실패
- publish 메시지의 persistent 속성 검증
- RabbitMQ integration test (가능 시): Testcontainers로 실제 queue 적재 확인

## 7) BFF 연동 E2E

1. completion queue / DLQ 프로비저닝
2. BFF 기동
3. Data Ingestion Worker completion event publish
4. BFF consumer consume
5. auth-server deactivate API 호출 확인
6. deactivate 실패 시 retry 후 DLQ 이동 확인

핵심 계약 한 줄 정리:

`default exchange("")` + `routingKey=lina.admin.ingest.completion` + credential 없는 `persistent` JSON event 발행.
