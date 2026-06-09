"""app.ingestion — Ingestion 파이프라인.

표준 PageObject를 검색 가능한 벡터로 변환해 Qdrant Multi-Pool에 적재한다.
처리 결과는 MongoDB ingestion_jobs 컬렉션에 단계별 상태로 기록한다.

단계 및 분류 (docs/rag-pipeline-design.md §3, §5):
- document_analyzer.py  문서 분석기 [Agent]     스페이스별 1회 doc_type 판별 → MySQL 캐싱
- attachment_analyzer.py 첨부 파일 분석기 [Pipeline] mime/확장자 판별 + 텍스트 유효성 검증
                          (메타 상속·청크 호출은 chunker / 그래프 노드 책임)
- chunker/              Adaptive Chunker [Pipeline]  본문 6유형 + 첨부 3유형 청킹 (하위 패키지)
- embedding.py          Dual Embedding [Pipeline]  Dense(e5-large 1024d) + Sparse(BM25)
- vector_store.py       Multi-Pool Vector Store [Storage]  Qdrant title/content/label pool upsert
- indexer.py            인덱싱 오케스트레이터 [Pipeline]  청크 → 임베딩 → upsert + chunk_lookup 적재
- sync.py               삭제 동기화 [Pipeline]  Reconciliation 중심 3중 전략 (고스트 데이터 방지)
- jobs.py               ingestion_jobs 상태 기록 헬퍼

구현 상태:
- attachment_analyzer.py  AttachmentAnalysisResult + analyze_attachment [feature6 Phase 1]
- sync.py                 ReconciliationResult + reconcile_deletions [feature6 Phase 3]
- chunker/                feature3·4 완료 (본문 6유형 + 첨부 PDF/Word/Excel/CSV)
- embedding.py / vector_store.py / indexer.py  feature5-A·5-B 완료
- document_analyzer.py    구현 완료 (featureI-7b — DocTypeClassifier/OpenAI·Fake + bootstrap 배선)
- jobs.py                 app/storage/jobs.py 로 이전 (외부 저장소 어댑터 일관성, feature6 Phase 2)
"""

from app.ingestion.attachment_analyzer import (
    AttachmentAnalysisResult,
    analyze_attachment,
)
from app.ingestion.sync import ReconciliationResult, reconcile_deletions

__all__ = [
    "AttachmentAnalysisResult",
    "ReconciliationResult",
    "analyze_attachment",
    "reconcile_deletions",
]
