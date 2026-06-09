# Claude Code Prompt Templates

이 문서는 Claude Code에게 작업을 지시할 때 사용하는 팀 공통 프롬프트 템플릿을 정의한다.

---

## 1. 기능 구현 시작 프롬프트

```text
다음 기능을 구현해줘.

[작업 목표]
-

[담당 영역]
- Backend / Frontend / RAG Pipeline / AI Agent 중 하나

[작업 범위]
-

[수정 가능 파일]
-

[수정 금지 파일]
-

[참고 문서]
- CLAUDE.md
- docs/architecture.md
- docs/conventions.md
- docs/api-spec.md
- docs/db-schema.md

[작업 규칙]
- 구현 전에 Plan을 먼저 작성해.
- Plan에는 수정 파일, 구현 단계, 테스트 계획, 문서 수정 필요 여부를 포함해.
- 아직 코드는 수정하지 마.
- 작업 범위를 벗어난 리팩토링은 하지 마.
```

---

## 2. Plan 작성 전용 프롬프트

```text
아래 작업을 구현하기 전에 Plan만 작성해줘.
아직 코드는 수정하지 마.

[작업 내용]
-

[Plan에 포함할 내용]
1. 작업 목표
2. 수정 대상 파일
3. 수정하지 않을 파일
4. 구현 단계
5. 테스트 계획
6. 문서 수정 필요 여부
7. 예상 위험 요소
8. 완료 기준
```

---

## 3. 테스트 우선 작성 프롬프트

```text
이 기능을 구현하기 전에 테스트 케이스를 먼저 작성해줘.

[기능 설명]
-

[요구 사항]
- Acceptance Criteria를 먼저 정리해.
- Unit Test, Integration Test, API Contract Test 중 필요한 테스트를 구분해.
- 실패하는 테스트를 먼저 작성해.
- 테스트 작성 후 아직 실제 구현은 하지 마.
```

---

## 4. 최소 구현 프롬프트

```text
앞서 작성한 테스트를 통과시키기 위한 최소 구현을 진행해줘.

[규칙]
- 테스트를 통과시키는 데 필요한 코드만 작성해.
- 불필요한 추상화나 대규모 리팩토링은 하지 마.
- 기존 코드 스타일과 docs/conventions.md를 따라.
- 구현 후 관련 테스트를 실행해.
```

---

## 5. 버그 수정 프롬프트

```text
아래 버그를 수정해줘.

[버그 설명]
-

[재현 방법]
1.
2.
3.

[기대 동작]
-

[현재 동작]
-

[규칙]
- 먼저 버그를 재현하는 테스트를 작성해.
- 원인을 분석한 뒤 최소 수정으로 해결해.
- 기존 정상 동작이 깨지지 않도록 관련 테스트를 실행해.
- 수정 후 원인과 변경 사항을 요약해.
```

---

## 6. 리팩토링 프롬프트

```text
아래 범위의 코드를 리팩토링해줘.

[리팩토링 목표]
-

[대상 파일]
-

[유지해야 할 동작]
-

[규칙]
- 외부 동작은 변경하지 마.
- public API는 변경하지 마.
- 테스트가 없는 동작은 임의로 바꾸지 마.
- 리팩토링 전후로 테스트를 실행해.
- 변경 이유와 개선 효과를 요약해.
```

---

## 7. 코드 리뷰 프롬프트

```text
아래 변경 사항을 코드 리뷰해줘.

[리뷰 기준]
- 요구사항을 충족하는가
- 아키텍처 규칙을 위반하지 않는가
- 계층 책임이 분리되어 있는가
- 테스트가 충분한가
- 예외 처리가 적절한가
- 보안상 위험한 부분이 없는가
- 불필요한 복잡성이 없는가

[출력 형식]
1. 반드시 수정해야 할 문제
2. 수정하면 좋은 문제
3. 잘 작성된 부분
4. 추가 테스트가 필요한 부분
```

---

## 8. API 변경 프롬프트

```text
아래 API를 추가 또는 변경해줘.

[API 설명]
-

[요청]
- Method:
- URL:
- Request Body:
- Query Parameter:
- Path Variable:

[응답]
- Success Response:
- Error Response:

[규칙]
- 구현 전 docs/api-spec.md를 확인해.
- API 변경 후 docs/api-spec.md를 함께 수정해.
- Controller 테스트 또는 API Contract 테스트를 작성해.
- 공통 응답 형식과 예외 처리 규칙을 따라.
```

---

## 9. DB 변경 프롬프트

```text
아래 DB 변경 작업을 진행해줘.

[변경 내용]
-

[대상 테이블]
-

[규칙]
- 구현 전 docs/db-schema.md를 확인해.
- Entity 변경 시 Migration을 함께 작성해.
- DB 변경 후 docs/db-schema.md를 수정해.
- 기존 데이터에 미치는 영향을 설명해.
- 관련 Repository 또는 Integration Test를 작성해.
```

---

## 10. RAG Pipeline 작업 프롬프트

```text
아래 RAG Pipeline 작업을 진행해줘.

[작업 목표]
-

[대상 단계]
- Ingestion / Chunking / Embedding / Retrieval / Reranking / Generation / Citation Verification 중 하나

[규칙]
- 기존 Pipeline 흐름을 먼저 확인해.
- ACL pre-filtering을 우회하지 마.
- 출처 없는 답변을 생성하는 방향으로 수정하지 마.
- Prompt 변경 시 변경 의도와 기대 효과를 기록해.
- Retrieval 설정 변경 시 평가 질문 기준으로 결과를 비교해.
```

---

## 11. AI Agent 작업 프롬프트

```text
아래 AI Agent 작업을 진행해줘.

[Agent 역할]
-

[입력]
-

[출력]
-

[연동 대상]
-

[규칙]
- Agent의 책임을 명확히 분리해.
- 다른 Agent의 역할을 침범하지 마.
- 실패 시 fallback 또는 error handling을 정의해.
- Prompt 변경 시 의도와 부작용 가능성을 기록해.
- 테스트 가능한 단위로 구현해.
```

---

## 12. Frontend 작업 프롬프트

```text
아래 Frontend 기능을 구현해줘.

[화면/컴포넌트]
-

[기능]
-

[사용 API]
-

[규칙]
- API Response 타입을 임의로 추정하지 마.
- docs/api-spec.md를 먼저 확인해.
- Loading, Error, Empty 상태를 함께 처리해.
- 공통 컴포넌트를 재사용해.
- 접근성 label, alt, keyboard interaction을 고려해.
```

---

## 13. 작업 완료 검증 프롬프트

```text
현재 변경 사항을 완료 전 검증해줘.

[확인할 내용]
- git diff 기준 변경 범위
- 요청 범위 외 변경 여부
- 테스트 통과 여부
- lint 통과 여부
- format 적용 여부
- 문서 수정 필요 여부
- 불필요한 로그나 임시 코드 여부

[실행할 명령]
- ./scripts/format.sh
- ./scripts/lint.sh
- ./scripts/test.sh
- ./scripts/verify.sh

[출력 형식]
1. 변경 요약
2. 실행한 명령
3. 테스트 결과
4. 남은 문제
5. 커밋 가능 여부
```
