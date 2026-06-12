"""app.ingestion.workers — RabbitMQ Worker (비동기 수집 파이프라인).

작성자 : 최태성
담당 영역 : ingestion

사용자 요청 트래픽과 분리된 RabbitMQ 큐를 소비해 수집을 처리하고 EKS에서 독립 스케일링한다
(요구사항정의서 §2.3, docs/architecture.md). 복사된 청킹/임베딩 자산(`app/ingestion/chunker`·
`embedder`·`indexer.py`)을 Worker 경계에서 호출한다.

PoC 토폴로지(featureI-4): **단일 ``chunking_worker``** 가 페이지/첨부 메시지를 받아 문서·첨부
분석 + Adaptive Chunking + Dual Embedding + Qdrant upsert 까지 수행한다(별도 attachment/embedding
worker 로 분리하지 않음). 삭제 트리거는 ``sync_worker`` 가 담당한다. 아래 큐 상수는 라우팅·DLQ
계약으로 보존한다(Quorum Queue 권장). pika 실행 loop·consumer 엔트리포인트는 featureI-7c 후속.
"""

QUEUE_INGESTION = "ingestion"
QUEUE_ATTACHMENT = "content.extract.attachment"
QUEUE_CHUNKING = "content.chunking"
QUEUE_EMBEDDING = "content.embedding"

__all__ = [
    "QUEUE_ATTACHMENT",
    "QUEUE_CHUNKING",
    "QUEUE_EMBEDDING",
    "QUEUE_INGESTION",
]
