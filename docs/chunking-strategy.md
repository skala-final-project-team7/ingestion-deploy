# Adaptive Chunking 전략 (구현 참조)

이 문서는 Adaptive Chunking 전략 설계서(`DESIGN-CHUNKING-2026-001 v0.1`, 작성자 최태성)를
구현 관점에서 정리한 저장소 내 참조 문서다. `docs/rag-pipeline-design.md` §3.4와 정합한다.
청킹 전략 변경 시 이 문서를 함께 갱신하고, 평가 쿼리 결과를 `docs/ai/working-log.md`에 기록한다.

---

## 1. 설계 원칙 (5대 원칙)

의미 단위 우선(고정 크기 분할 회피), 원자성 유지(Q&A·ADR·회의록 안건은 길이 초과해도 분할 금지),
맥락 동봉(상위 섹션 제목·시트명·컬럼명을 청크 도입부에 부착), 멱등성(동일 입력 → 동일 chunk_id),
복원 가능성(청크 → 원본 섹션·페이지·시트 역추적 가능).

## 2. 컴포넌트 분류

| 컴포넌트 | 분류 |
|---|---|
| 문서 분석기 (본문 doc_type 판별) | Agent |
| 첨부 파일 분석기 (attachment_type 판별) | Pipeline |
| Adaptive Chunker (분할 실행) | Pipeline |
| 메타데이터 부착기 | Pipeline |
| 하한선 병합기 | Pipeline |

## 3. 2단계 하이브리드 분할 규칙

| 단계 | 처리 |
|---|---|
| 1차 분할 | `doc_type`/`attachment_type`별 **논리 단위 파서**로 분할 |
| 2차 재분할 | 1차 결과 중 `token_count > 800`인 청크를 100토큰 오버랩 슬라이딩 윈도우로 재분할. **원자성 유지 유형은 제외** |
| 하한선 처리 | `token_count < 200`인 청크는 직전/직후 동일 `doc_type` 청크와 병합. **원자성 유지 유형은 제외** |

임계값 근거: 800토큰 = multilingual-e5-large 권장 입력(512~1024)의 중간값 · 100토큰 오버랩 =
인접 청크 맥락 끊김 방지 · 200토큰 = 임베딩 품질 하한.

## 4. 본문 6유형 청킹 전략

공통 전처리: BeautifulSoup(lxml) HTML 파싱(실패 시 plain text fallback `PARTIAL_PARSE`),
Confluence 매크로 정규화(내용만 추출), 코드 블록 ``` 펜스 보존, `<table>` → 마크다운,
이미지·도형은 alt/caption만 보존, 스마트 따옴표·공백 정규화.

| doc_type | 1차 분할 단위 | 원자성 | section_header |
|---|---|---|---|
| `incident` (장애대응) | 타임라인 / 원인 / 해결 / 재발방지 4블록 | 각 블록 | 블록명 |
| `operation` (운영매뉴얼) | H2 섹션 (H3 → 단락 fallback) | — | H2/H3 텍스트 |
| `faq` | Q&A 쌍 1개 = 청크 1개 | **Q&A 쌍 분리 금지** | 질문 원문(`?` 포함) |
| `meeting` (회의록) | 안건 단위. 상단 메타(일자·참석자)는 각 안건 도입부에 부착 | 안건 (1500토큰 초과 시만 예외 분할) | `안건 N: <제목>` |
| `adr` | ADR 전체 = 1청크 | **분할 금지** (2000토큰 초과 시만 2분할) | `ADR-NNNN │ <제목>` |
| `troubleshoot` (트러블슈팅) | 증상-원인-해결 케이스 단위 | 케이스 (800토큰 초과 시 해결 블록만 분리) | `증상: <요약>` |

doc_type 미결정 시 `operation` 기본값.

## 5. 첨부 파일 3유형 청킹 전략

| attachment_type | 추출 도구 | 1차 분할 | 직렬화 / 비고 |
|---|---|---|---|
| `pdf` | PyMuPDF(fitz) → pdfplumber fallback | 섹션 휴리스틱(폰트 크기·굵기·짧은 행). 미검출 시 800토큰 슬라이딩 윈도우 | `extracted_format=raw_text`, section_header=`p.<N>: <제목>` |
| `docx` | python-docx (스타일 보존) | Heading 1/2/3 → 단락 fallback. 표는 마크다운 변환 | `extracted_format=raw_text`, section_header=Heading 텍스트 |
| `xlsx` / `csv` | openpyxl + pandas / pandas(인코딩 자동감지) | 시트 단위 → 시트 내 N행 그룹(기본 50행, 800토큰 초과 시 25→10행 축소) | `extracted_format=sheet_serialized`, section_header=`[시트명] 행 N~M` |

**Excel/CSV 자연어 직렬화** — 각 행을 `[<시트명>] <컬럼1>: <값1> | <컬럼2>: <값2> | ...` 형식으로
직렬화한다. 컬럼명을 매 행에 부착하면 (a) Dense 임베딩이 의미 토큰+수치 토큰을 함께 학습,
(b) BM25가 키워드를 정확 매칭, (c) LLM이 수치의 단위·시점을 추론할 수 있다. 컬럼명 헤더 행은
매 청크마다 반복 부착. 컬럼명이 단위와 분리된 경우 자동 결합(`비용` + `단위: USD` → `비용(USD)`).
빈 셀은 직렬화에서 생략. 병합 셀은 모든 셀에 동일 값 복제.

비텍스트 콘텐츠(이미지·도형·차트·다이어그램·임베디드 객체)는 PoC 색인 대상에서 제외 —
alt·title·caption 텍스트만 인접 텍스트로 보존(향후 멀티모달 확장).

## 6. 청크 메타데이터 (19종)

**공통 13종** — `chunk_id`(SHA1(page_id+chunk_index+attachment_id), 결정론적),
`page_id`, `page_title`, `section_header`, `section_path`, `chunk_index`(0-based), `labels[]`,
`doc_type`, `space_key`, `allowed_groups[]`, `allowed_users[]`, `webui_link`, `last_modified`.

**첨부 전용 5종 (v0.2.2)** — `source_type`(`page`|`attachment`, 모든 청크 필수),
`attachment_id`, `attachment_filename`, `attachment_mime`, `extracted_format`.

**검증용 1종** — `token_count`(임베딩 토큰 기준, 800토큰 임계 판단).

무결성 규칙: ACL 누락 청크 색인 금지 · `section_header` 빈 문자열 금지(`untitled` 부착) ·
`chunk_id`는 결정론(임의 UUID 금지) · `doc_type` 미결정 시 `operation` fallback ·
`labels`는 lowercase + 하이픈 정규화.

## 7. 토큰 카운팅

| 용도 | 토크나이저 | 메타 필드 |
|---|---|---|
| 임베딩 토큰 (분할 임계) | multilingual-e5-large → SentencePiece | `token_count` (필수) |
| LLM 토큰 (컨텍스트 계산) | tiktoken `cl100k_base` | `llm_token_count` (선택) |
| BM25 토큰 (Sparse 인덱스) | KoNLPy Mecab 또는 Kiwi 형태소 분석기 | 메타 미부착 (Sparse 벡터에 직접 인코딩) |

## 8. 예외 처리 (`ingestion_jobs` 상태 코드)

`PARTIAL_PARSE`(HTML 파싱 실패) · `EMPTY_BODY` / `EMPTY_BODY_ATTACH_ONLY`(빈 본문) ·
`INVALID_ACL`(ACL 누락) · `UNSUPPORTED_ATTACH_TYPE`(미지원 mime) · `ATTACH_ENCRYPTED`(암호화 PDF) ·
`LOW_QUALITY_ATTACH`(텍스트 품질 미달) · `ATTACH_NO_HEADER`(Excel 헤더 누락 → `col_1,col_2,...` 부여) ·
`OVERSIZE_ATOMIC`(원자성 유형 1500토큰 초과 → 강제 분할 + `Part N/M` 표기) ·
`ATTACH_DOWNLOAD_FAILED`(첨부 다운로드 실패 — 재시도 소진/URL 검증 거부, FR-002 후속 2026-06-10) ·
`TOKENIZER_FAIL`(토크나이저 실패 → DLQ 후 재시도).

## 9. 품질 KPI

본문 맥락 단절율 0건 · 원자성 위반 0건 · 첨부(PDF/Word) 섹션 헤더 보존율 ≥ 90% ·
첨부(Excel/CSV) 컬럼명 동봉률 ≥ 95% · 필수 메타 결측률 0건 · token_count 분포 95% in [200, 800].
