# Architecture Decision Records (ADR)

이 디렉토리는 RAG Pipeline의 주요 아키텍처 결정을 기록한다.

ADR은 "왜 이 선택을 했는가"를 남기는 문서다. 임베딩 모델 선택, 청킹 전략,
Retrieval 방식(Hybrid/Dense), Reranking 모델, 출처 검증 방식 등
되돌리기 어렵거나 영향 범위가 큰 결정을 작성한다.

## 작성 시점

- 아키텍처 변경이 필요한 작업의 Plan 단계에서 작성한다 (`docs/architecture.md` 12절 참고).
- 대안 비교와 선택 이유를 포함한다.

## 파일명 규칙

```
NNNN-<짧은-제목>.md   예: 0001-use-hybrid-retrieval.md
```

## 템플릿

```md
# NNNN. <결정 제목>

- 상태: 제안 / 채택 / 폐기
- 날짜: YYYY-MM-DD
- 작성자:

## 배경
<어떤 문제/제약 때문에 결정이 필요한가>

## 검토한 대안
<대안 A / 대안 B / 대안 C 와 장단점>

## 결정
<무엇을 선택했는가>

## 영향
<영향 범위, 후속 작업, 함께 수정한 문서>
```
