"""스캐폴드 스모크 테스트.

초기 구조가 import 가능하고 공유 스키마·신규 stub 의 계약이 살아있는지 확인한다.
실행 전 의존성 설치 필요: ``pip install -e ".[ingestion,embedding,dev]"`` (Python 3.11).
"""


def test_schemas_importable_and_chunk_id_deterministic() -> None:
    """공유 스키마(복사 자산)가 import 되고 make_chunk_id 가 결정론적이다."""
    from app.schemas import make_chunk_id

    a = make_chunk_id("PAGE-1", 0)
    b = make_chunk_id("PAGE-1", 0)
    assert a == b
    assert make_chunk_id("PAGE-1", 1) != a


def test_crawler_public_contract_available() -> None:
    """FR-001 Full Crawl 은 featureI-6(vendored Data Ingestion Agent)로 구현됨.

    상세 동작은 tests/ingestion/test_crawler.py·tests/adapters/test_atlassian.py 에서 검증.
    여기서는 공개 계약(CrawlRequest/CrawlResult/run_full_crawl)이 import 가능한지만 확인한다.
    """
    from app.ingestion.crawler import CrawlRequest, CrawlResult, run_full_crawl

    assert callable(run_full_crawl)
    assert CrawlRequest(space_key="CPC").space_key == "CPC"
    assert CrawlResult(space_key="CPC").pages_collected == 0


def test_extractor_implemented_contract() -> None:
    """FR-002 첨부 추출기는 featureI-3 에서 구현됨(CSV 는 stdlib 만 사용 — 외부 의존성 불필요).

    유형별 상세는 tests/ingestion/test_attachment_extractor.py 에서 검증.
    """
    from app.ingestion.extractor import ExtractionResult, extract_attachment_text
    from app.schemas.enums import AttachmentType

    result = extract_attachment_text(
        attachment_id="att-1",
        attachment_type=AttachmentType.CSV,
        content=b"region,sales\nKR,100\n",
    )
    assert isinstance(result, ExtractionResult)
    assert result.ok is True
    assert "region: KR, sales: 100" in result.text


def test_worker_queue_names_defined() -> None:
    """RabbitMQ 큐 이름 상수가 정의돼 있다(Worker 배선 전 계약)."""
    from app.ingestion.workers import (
        QUEUE_ATTACHMENT,
        QUEUE_CHUNKING,
        QUEUE_EMBEDDING,
        QUEUE_INGESTION,
    )

    assert {QUEUE_INGESTION, QUEUE_ATTACHMENT, QUEUE_CHUNKING, QUEUE_EMBEDDING}
