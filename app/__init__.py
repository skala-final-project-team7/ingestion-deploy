"""app — Data Ingestion Pipeline (척척학사/LINA).

작성자 : 최태성
담당 영역 : ingestion

Confluence 문서·첨부를 수집(Crawl/Delta Sync)하여 첨부 텍스트 추출 → Adaptive Chunking →
Dual Embedding 색인까지 수행하는 비동기 수집 파이프라인. RAG Pipeline(질의/응답)과 분리된
독립 배포 단위이며, RabbitMQ 큐(Ingestion / Attachment / Chunking / Embedding)로 단계를 잇는다.

패키지 구성:
    app/
    ├── config.py        환경 설정 (Confluence / Qdrant / MongoDB / MySQL / RabbitMQ)
    ├── schemas/         계층 간 데이터 계약 (PageObject / Chunk / enums) — RAG 레포와 공유 모델
    ├── adapters/        문서 공급원 어댑터 (JsonFixture / Atlassian) [DocumentSourceAdapter]
    ├── ingestion/       수집·청킹·임베딩 코어
    │   ├── crawler.py            Data Ingestion Agent — Confluence Full Crawl (FR-001)
    │   ├── extractor/            첨부 텍스트 추출기 (PDF/Word/Excel, FR-002)
    │   ├── chunker/              Adaptive Chunker (본문 6유형 + 첨부 3유형, FR-003)
    │   ├── embedder/             Dense(e5-large) / Sparse(BM25) 임베더
    │   ├── embedding.py          Dual Embedding 입력·payload·멱등성 (FR-004)
    │   ├── vector_store.py       Qdrant Multi-Pool upsert [Storage]
    │   ├── indexer.py            청크 → 임베딩 → upsert 오케스트레이터
    │   ├── attachment_analyzer.py 첨부 mime/유형 판별 [Pipeline]
    │   ├── sync.py               삭제 동기화 (Reconciliation 3중 전략)
    │   └── workers/              RabbitMQ 컨슈머 — chunking·sync Worker 구현
    └── storage/         MongoDB(raw_pages/raw_attachments/jobs/cache) · Qdrant 클라이언트

> chunker·embedder·schemas·storage·adapters 는 RAG 레포(skala-final/rag)에서 복사한 자산이다
> (2026-05-26 분리). 공통 모델이 갈라지면 양 레포 동기화에 유의한다 (docs/ai/current-plan.md).
"""
