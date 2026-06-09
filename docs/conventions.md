이 문서는 프로젝트의 공통 코딩 컨벤션을 정의한다.  
Backend, Frontend, RAG Pipeline, AI Agent 작업자는 이 문서를 기준으로 코드를 작성한다.

---

## 1. 공통 원칙

- 명확성이 짧은 코드보다 우선한다.
- 기존 코드 스타일을 우선적으로 따른다.
- 불필요한 추상화보다 현재 요구사항을 정확히 해결하는 구조를 우선한다.
- 하나의 함수와 클래스는 하나의 책임을 갖는다.
- 중복 제거를 이유로 서로 다른 도메인 개념을 무리하게 합치지 않는다.
- 임시 구현, 우회 로직, 의미 없는 TODO를 남기지 않는다.
- 외부 동작 변경과 리팩토링을 한 커밋에 섞지 않는다.

---

## 2. Formatting

Formatting은 도구로 자동 처리 가능한 코드 스타일을 의미한다.

포함 항목:

- indent
- line break
- blank line
- import order
- trailing comma
- line length
- EOF newline

규칙:

- 커밋 전 `./scripts/format.sh`를 실행한다.
- formatter 결과를 수동으로 되돌리지 않는다.
- 포맷팅 변경과 기능 변경은 가능하면 분리한다.
- 불필요한 스타일 논쟁은 formatter 기준을 따른다.

---

## 3. Linting

Linting은 코드 품질, 잠재 오류, 팀 규칙 위반을 검사하는 작업이다.

포함 항목:

- unused import
- unused variable
- null 위험
- 복잡도 초과
- 불필요한 조건문
- 잠재 버그
- 보안 취약 패턴
- 타입 오류

규칙:

- 커밋 전 `./scripts/lint.sh`를 실행한다.
- lint 에러를 우회하기 위해 규칙을 임의로 비활성화하지 않는다.
- 규칙 비활성화가 필요할 경우 이유를 주석 또는 문서에 남긴다.

---

## 4. Naming

### 4.1 공통 네이밍

- 변수명은 의미를 드러내도록 작성한다.
- 축약어는 널리 쓰이는 경우가 아니면 피한다.
- Boolean 변수는 `is`, `has`, `can`, `should` prefix를 사용한다.
- Collection 변수는 복수형을 사용한다.
- 임시 변수명인 `data`, `info`, `temp`, `result`는 의미가 불명확하면 사용하지 않는다.

좋은 예:

```text
userId
conversationId
hasPermission
retrievedDocuments
embeddingRequest
```

나쁜 예:

```text
data
info
tmp
flag
list
```

---

## 5. Comments

주석은 코드의 가독성과 유지보수성을 높이기 위한 문서화 수단이다.  
LINA 프로젝트에서는 단순히 코드가 “무엇을 하는지”를 반복하는 주석보다, **왜 이런 구조를 선택했는지**, **어떤 제약을 고려했는지**, **호출자가 무엇을 알아야 하는지**를 설명하는 주석을 우선한다.

RAG Pipeline, AI Agent, Backend, Frontend는 변경이 자주 발생하므로 주요 클래스/모듈/public 함수에는 표준 주석 블록을 작성한다.

---

### 5.1 표준 주석 블록

다음 대상에는 표준 주석 블록을 작성한다.

- 주요 클래스
- 주요 모듈
- public API 함수
- Agent Node 함수
- 외부 API Adapter
- Repository / Data Access 객체
- RAG Pipeline의 주요 단계 함수

표준 주석 블록에는 다음 정보를 포함한다.

- 작성자
- 작성목적
- 작성일
- 변경사항 내역
- 호환성

단, private helper 함수나 단순 getter/setter처럼 의미가 명확한 코드는 표준 주석 블록을 생략할 수 있다.

---

#### Python 표준 주석

```python
"""
--------------------------------------------------
작성자 : 최태성
작성목적 : LINA RAG 파이프라인의 질의 라우터(Query Router) 에이전트 구현.
          단일 LLM 호출로 Intent Router / Query Rewriter / Filter Builder 세 하위 기능을 동시 수행.
작성일 : 2026-05-12
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-12, 최초 작성, LangGraph 노드 형태로 query_router_agent 구현
  - 2026-05-15, 의도 분류 정확도 개선, 4가지 유형(장애 대응/운영/정책/이력)으로 명세 강화
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - LangGraph 0.2.x, LangChain 0.3.x 기준
  - openai>=1.30 기준
--------------------------------------------------
"""
from typing import Literal
from langgraph.graph import StateGraph
from app.schemas.rag_state import RagState


def build_query_router(state: RagState) -> RagState:
    """질의 라우터: 단일 LLM 호출로 의도 분류, 쿼리 확장, 필터 빌드를 수행한다."""
    ...
```

---

#### Java 표준 주석

```java
/**
 * --------------------------------------------------
 * 작성자 : 최태성
 * 작성목적 : LINA BFF의 채팅 메시지 송수신 컨트롤러 구현.
 *           SSE(Server-Sent Events) 기반 스트리밍 응답을 ML Pipeline으로부터 중계.
 * 작성일 : 2026-05-13
 * 변경사항 내역 (날짜, 변경목적, 변경내용 순)
 *   - 2026-05-13, 최초 작성, 기본 메시지 전송/조회 엔드포인트 구현
 *   - 2026-05-19, SSE 스트리밍 도입, sendMessage() 반환 타입을 SseEmitter로 변경
 * --------------------------------------------------
 * [호환성]
 *   - JDK 21 LTS 권장 — Virtual Threads 사용
 *   - Spring Boot 3.3.x, Spring MVC 6.1.x 기준
 *   - Spring Cloud Gateway Server MVC 4.1.x와 라우팅 호환
 * --------------------------------------------------
 */
@RestController
@RequestMapping("/api/v1/cb/cba/chat")
@RequiredArgsConstructor
public class ChatController {
    private final ChatService chatService;
}
```

---

#### TypeScript / Vue 표준 주석

```ts
/**
 * --------------------------------------------------
 * 작성자 : 최태성
 * 작성목적 : LINA 챗봇 메인 화면(CHAT-01)의 SSE 스트리밍 응답 처리 컴포저블.
 *           BFF의 /api/v1/cb/cba/chat/messages 엔드포인트를 EventSource로 구독한다.
 * 작성일 : 2026-05-22
 * 변경사항 내역 (날짜, 변경목적, 변경내용 순)
 *   - 2026-05-22, 최초 작성, useSseChat 컴포저블 구현
 *   - 2026-05-26, 인용 출처 카드 렌더링 보강, citation payload 파서 추가
 * --------------------------------------------------
 * [호환성]
 *   - Node.js 20.x LTS, TypeScript 5.4+
 *   - Vue 3.4.x + Pinia 2.1.x
 *   - Vite 5.x dev server / Tailwind CSS 3.4.x
 * --------------------------------------------------
 */
import { ref } from 'vue';
import type { ChatMessage, Citation } from '@/types/chat';

export function useSseChat() {
  // ...
}
```

---

### 5.2 표준 주석 블록 작성 규칙

- 주요 클래스/모듈/public 함수 상단에는 표준 주석 블록을 작성한다.
- 작성자, 작성목적, 작성일은 누락하지 않는다.
- 변경사항은 `날짜, 변경목적, 변경내용` 형식으로 작성한다.
- 호환성 섹션에는 사용 언어, 프레임워크, 런타임의 권장 버전을 작성한다.
- 언어가 바뀌더라도 작성자, 작성목적, 작성일, 변경사항, 호환성 정보는 동일하게 유지한다.
- PR 리뷰 시 주요 코드의 표준 주석 블록 누락은 수정 요청 대상이다.
- 변경사항 내역은 중요한 구조 변경, public API 변경, 호환성 영향이 있는 변경 중심으로 작성한다.
- 단순 오타 수정, formatter 적용, import 정리까지 변경사항 내역에 모두 기록하지 않는다.

---

### 5.3 함수 주석

표준 주석 블록과 별개로, public 함수에는 호출자를 위한 사용 설명 Docstring 또는 Javadoc을 작성한다.

함수 주석에는 다음 내용을 포함한다.

- 함수의 목적
- 주요 파라미터
- 반환 값
- 발생 가능한 예외
- 호출 시 주의 사항

#### Java Javadoc

```java
/**
 * 사용자의 자연어 질문을 받아 RAG 파이프라인에 위임하고, 결과를 SSE 스트림으로 반환한다.
 *
 * @param request 사용자 질문 및 대화 컨텍스트 정보
 * @param user 인증된 사용자 정보
 * @return SseEmitter — 답변 토큰, 인용 출처, 검증 결과를 순차 전송
 * @throws BizException SSE 연결 수립 실패 또는 ML Pipeline 호출 실패 시
 */
public SseEmitter streamAnswer(ChatRequest request, LinaUserPrincipal user) {
    ...
}
```

#### Python Google-style Docstring

```python
def select_top_k_chunks(query: str, top_k: int = 5, *, acl: AclFilter) -> list[Chunk]:
    """ACL이 적용된 Hybrid Search를 수행하여 Top-K 청크를 반환한다.

    Args:
        query: 사용자의 자연어 질의 또는 Query Rewriter가 확장한 쿼리.
        top_k: 반환할 청크 수. 기본값 5.
        acl: ACL Pre-filter. allowed_groups, allowed_users 정보를 포함한다.

    Returns:
        관련도 점수 기준으로 내림차순 정렬된 Top-K 청크 리스트.

    Raises:
        QdrantConnectionError: Qdrant 호출 실패 시.
    """
    ...
```

---

### 5.4 변수 주석

변수 주석은 변수명과 타입만으로 의미를 파악하기 어려운 경우에만 작성한다.

규칙:

- 변수명으로 의미를 표현하는 것을 우선한다.
- Python 타입 힌트, TypeScript 타입 명시, Java `final` 등을 활용해 주석 의존도를 낮춘다.
- 라인 끝 주석은 짧고 구체적으로 작성한다.
- 모든 변수에 습관적으로 주석을 달지 않는다.

예시:

```java
private final int maxTopK = 20;             // 1차 Hybrid Search 후보 수
private final long sseTimeoutMs = 60_000L;  // SSE 연결 유지 최대 시간
```

```python
max_top_k: int = 20                  # 1차 Hybrid Search 후보 수
sse_timeout_seconds: float = 60.0    # SSE 응답 keep-alive 한계
```

---

### 5.5 제어 구조 주석

복잡한 조건문, 반복문, 분기 처리에는 직전에 한 줄 요약 주석을 작성한다.

규칙:

- 조건의 배경이나 정책적 이유를 설명한다.
- 단순한 if/for 문에는 주석을 달지 않는다.
- 매직 넘버는 상수로 추출한다.
- 상수 추출이 어려운 경우 주석으로 의미를 보완한다.

예시:

```python
# 1단계 규칙 기반 검증기에서 의심 문장으로 분류된 경우에만 2단계 LLM 평가자를 호출한다.
if rule_verification.has_suspicious_sentences():
    llm_verification = llm_verifier.evaluate(rule_verification.suspicious_sentences)
    final_result = merge_verification_results(rule_verification, llm_verification)
else:
    final_result = rule_verification
```

---

### 5.6 데이터 액세스 주석

Repository, Adapter, Client 계층의 주요 메서드에는 호출 의도와 조회 패턴을 주석으로 작성한다.

포함할 내용:

- 호출 목적
- 사용 인덱스
- 정렬 기준
- 필터 조건
- 호출 위치
- 성능상 주의점

저장소별 권장 내용:

- MySQL Query: 사용 인덱스 컬럼, 정렬 기준, 페이징 여부
- MongoDB Query: 사용 인덱스, 필터 조건, projection 여부
- Vector DB / Qdrant Query: filter, pool, top-k, score 기준
- External API Adapter: rate limit, retry 정책, timeout 정책

예시:

```java
/**
 * 사용자별 최근 대화 목록을 페이징하여 조회한다.
 *
 * - 사용 인덱스: idx_messages_user_created_at (user_id, created_at DESC)
 * - 정렬: created_at DESC
 * - 호출 위치: ChatService.selectConversationList
 */
List<ConversationDocument> findByUserIdOrderByCreatedAtDesc(
        String userId,
        Pageable pageable
);
```

---

### 5.7 TODO / FIXME / NOTE / HACK

임시 작업, 알려진 결함, 구현 배경은 정해진 주석 마커를 사용해 작성한다.

| Marker | 의미 | 형식 |
|---|---|---|
| TODO | 향후 구현 예정 사항 | `TODO(작성자, YYYY-MM-DD): 설명 — 이슈번호` |
| FIXME | 알려진 결함 또는 수정 필요 사항 | `FIXME(작성자, YYYY-MM-DD): 설명 — 이슈번호` |
| NOTE | 코드 의도 또는 배경 설명 | `NOTE: 설명` |
| HACK | 임시 우회 코드 | `HACK(작성자, YYYY-MM-DD): 설명 — 이슈번호` |

규칙:

- TODO, FIXME, HACK에는 작성자와 날짜를 포함한다.
- HACK은 반드시 GitHub Issue 번호를 포함한다.
- FIXME는 가능한 경우 재현 조건을 함께 남긴다.
- 완료된 TODO, FIXME, HACK은 제거한다.

예시:

```java
// TODO(최태성, 2026-05-25): GPT-4o-mini 평가자 캐싱 로직 추가 — LINA-87
// FIXME(권서현, 2026-05-21): Qdrant Hybrid Search 점수 정규화 필요 — LINA-91
// NOTE: ms-marco-MiniLM-L-12 Cross-Encoder는 한 번에 최대 32쌍 추론한다.
// HACK(이다연, 2026-05-22): Confluence Trash API rate limit 회피를 위한 임시 sleep 50ms — LINA-92
```

---

### 5.8 좋은 주석과 나쁜 주석

좋은 주석은 코드만으로 알기 어려운 제약, 의도, 배경을 설명한다.

좋은 예:

```java
// Confluence API rate limit으로 인해 Space 단위가 아니라 Page 단위로 작업을 분리한다.
```

나쁜 예:

```java
// user를 저장한다.
userRepository.save(user);
```

---

### 5.9 주석 작성 금지 사항

- 코드가 그대로 설명하는 내용을 반복하지 않는다.
- 오래된 주석을 방치하지 않는다.
- 주석으로 잘못된 코드를 정당화하지 않는다.
- 민감 정보, 토큰, 비밀번호, 개인정보 원문을 주석에 남기지 않는다.
- 테스트 실패나 예외 무시를 주석으로 합리화하지 않는다.
- 모든 라인에 과도하게 주석을 달지 않는다.

---

## 6. Backend Convention

### 6.1 Layer 책임

| Layer | 책임 |
|---|---|
| Controller | Request 검증, Response 변환, 인증 사용자 정보 전달 |
| Service | 비즈니스 로직, 트랜잭션 관리 |
| Repository | DB 접근 |
| Client | 외부 API 호출 |
| DTO | 계층 간 데이터 전달 |
| Entity | 도메인 상태와 DB 매핑 |

규칙:

- Controller에 비즈니스 로직을 작성하지 않는다.
- Service에서 HTTP Request/Response 객체에 직접 의존하지 않는다.
- Entity를 API Response로 직접 반환하지 않는다.
- 외부 API 호출은 Client 계층으로 분리한다.
- 트랜잭션은 Service 계층에서 관리한다.

---

### 6.2 DTO 규칙

- 요청 DTO는 `Request` suffix를 사용한다.
- 응답 DTO는 `Response` suffix를 사용한다.
- 내부 전달 객체는 필요 시 `Command`, `Result` suffix를 사용한다.
- Entity와 DTO 변환 책임을 명확히 둔다.

예시:

```text
CreateConversationRequest
ConversationResponse
CreateMessageCommand
RagAnswerResult
```

---

### 6.3 Exception 규칙

- 예외는 공통 예외 처리 구조를 따른다.
- 비즈니스 예외와 시스템 예외를 구분한다.
- 외부 API 실패는 원인과 대상 시스템을 로그에 남긴다.
- 사용자에게 내부 구현 정보나 민감 정보를 노출하지 않는다.

---

### 6.4 Logging 규칙

로그에는 다음 정보를 포함할 수 있다.

- 요청 ID
- 사용자 ID
- 주요 도메인 ID
- 외부 API 이름
- 실패 원인
- 처리 시간

로그에 포함하면 안 되는 정보:

- Access Token
- Refresh Token
- Password
- Secret Key
- 개인정보 원문
- 전체 Prompt 원문
- 민감한 문서 내용 원문

---

## 7. Frontend Convention

- API Response 타입을 임의로 추정하지 않는다.
- 서버 상태와 UI 상태를 분리한다.
- Loading, Error, Empty 상태를 함께 처리한다.
- 공통 컴포넌트를 우선 재사용한다.
- 페이지 컴포넌트에 복잡한 비즈니스 로직을 몰아넣지 않는다.
- 접근성을 고려해 label, alt, keyboard interaction을 작성한다.
- API 변경이 필요하면 `docs/api-spec.md`를 먼저 수정한다.

---

## 8. RAG Pipeline Convention

- Ingestion, Chunking, Embedding, Retrieval, Reranking, Generation, Citation Verification 단계를 분리한다.
- ACL pre-filtering을 우회하지 않는다.
- 출처 없는 답변을 생성하는 방향으로 수정하지 않는다.
- Chunking 전략 변경 시 변경 이유와 기대 효과를 기록한다.
- Prompt 변경 시 변경 의도와 부작용 가능성을 기록한다.
- Retrieval 설정 변경 시 평가 질문 결과를 비교한다.
- 실험성 코드는 production path에 직접 연결하지 않는다.

---

## 9. AI Agent Convention

- Agent는 하나의 명확한 책임을 가진다.
- Agent 간 입력과 출력 형식을 명확히 정의한다.
- Prompt는 역할, 입력, 출력, 제약 조건을 포함한다.
- 실패 가능성이 있는 Agent는 fallback 또는 error handling을 정의한다.
- Agent가 DB나 외부 API에 직접 접근해야 하는 경우 책임 범위를 문서화한다.
- 답변 검증 Agent는 생성 Agent와 책임을 분리한다.

---

## 10. API Convention

- REST API는 리소스 중심으로 설계한다.
- HTTP Method 의미를 지킨다.
- 성공 응답과 실패 응답 형식을 통일한다.
- 인증이 필요한 API는 명확히 표시한다.
- API 변경 시 `docs/api-spec.md`를 함께 수정한다.

예시 응답 형식:

```json
{
  "success": true,
  "data": {},
  "message": null
}
```

예시 에러 형식:

```json
{
  "success": false,
  "error": {
    "code": "CONVERSATION_NOT_FOUND",
    "message": "대화를 찾을 수 없습니다."
  }
}
```

---

## 11. DB Convention

- 테이블명은 복수형 또는 팀에서 정한 기준을 일관되게 사용한다.
- 컬럼명은 snake_case를 사용한다.
- PK는 명확한 이름을 사용한다.
- FK 관계는 문서에 기록한다.
- 인덱스 추가 시 목적을 함께 기록한다.
- Entity 변경 시 Migration과 `docs/db-schema.md`를 함께 수정한다.

---

## 12. Test Convention

### 12.1 테스트 작성 기준

- 핵심 비즈니스 로직은 Unit Test를 작성한다.
- API는 Request/Response 검증 테스트를 작성한다.
- DB Query나 Migration은 Integration Test를 작성한다.
- 버그 수정 시 재현 테스트를 먼저 작성한다.

---

### 12.2 테스트 이름

테스트 이름은 의도를 드러내야 한다.

예시:

```text
shouldCreateConversationWhenValidRequest()
shouldReturnNotFoundWhenConversationDoesNotExist()
shouldRejectRequestWhenUserHasNoPermission()
```

한글 테스트명을 사용할 수 있다.

```text
유효한_요청이면_대화를_생성한다
존재하지_않는_대화이면_404를_반환한다
권한이_없으면_요청을_거부한다
```

---

## 13. Commit Convention

커밋 메시지는 다음 형식을 사용한다.

```text
type(scope): summary
```

예시:

```text
feat(backend): add conversation create api
fix(rag): handle empty retrieval result
test(backend): add conversation service test
docs(api): update chat api spec
refactor(frontend): extract chat input component
```

사용 가능한 type:

| Type | 의미 |
|---|---|
| feat | 기능 추가 |
| fix | 버그 수정 |
| refactor | 동작 변경 없는 구조 개선 |
| test | 테스트 추가/수정 |
| docs | 문서 수정 |
| chore | 설정, 빌드, 기타 작업 |
| style | 포맷팅 변경 |
