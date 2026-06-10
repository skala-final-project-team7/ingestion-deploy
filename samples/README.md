# samples/ — PoC 픽스처 (P1-6 후속, 2026-06-10)

`Settings.source_type="json_fixture"`(기본)의 `JsonFixtureSourceAdapter` 가 읽는 기본 픽스처.
`rag/samples/` 와 동일 사본이다(공유 자산 — 갱신 시 양쪽 동기화).

- `confluence_sample_data.json` / `datadog_docs.json` — Atlassian-Python-API 응답 포맷 페이지
- `attachments/` — 첨부 청킹 경로용 실제 파일(docx/xlsx)

기본 `POST /ml/ingest` 가 이 디렉터리 부재로 FileNotFoundError 로 죽던 문제(코드 리뷰 06-08 P1-6)의 해결로 추가됐다.
