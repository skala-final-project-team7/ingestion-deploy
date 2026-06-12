"""run_full_crawl 단위 테스트 — 어댑터→raw_pages 적재→Chunking Queue 발행 흐름 검증.

작성자 : 최태성
담당 영역 : ingestion

공급원 어댑터·raw_store·publisher 를 모두 fake 로 주입해 crawler 오케스트레이션만
검증한다(외부 의존성 mock — 루트 CLAUDE.md 테스트 규칙).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from app.adapters.base import ActiveIds, ChangeEvent, DocumentSourceAdapter
from app.ingestion.crawler import CrawlRequest, run_full_crawl
from app.ingestion.workers import QUEUE_CHUNKING
from app.ingestion.workers.publisher import FakeQueuePublisher
from app.schemas.enums import ExtractedFormat, IngestionStage, IngestionStatus
from app.schemas.page_object import Attachment, PageObject
from app.storage.jobs import FakeIngestionJobsRepository
from app.storage.raw_store import FakeRawPageStore


def _attachment(attachment_id: str, *, page_id: str) -> Attachment:
    return Attachment(
        attachment_id=attachment_id,
        filename=f"{attachment_id}.pdf",
        mime_type="application/pdf",
        extracted_text="x" * 250,
        extracted_format=ExtractedFormat.RAW_TEXT,
        download_url=f"https://confluence.example/download/{attachment_id}",
        parent_page_id=page_id,
        last_modified=datetime.fromisoformat("2026-05-14T01:00:00+00:00"),
    )


def _page(
    page_id: str,
    *,
    space_key: str = "ENG",
    version: int = 1,
    attachments: list[Attachment] | None = None,
) -> PageObject:
    return PageObject(
        page_id=page_id,
        space_key=space_key,
        title=f"Title {page_id}",
        body_html=f"<p>{page_id}</p>",
        version_number=version,
        last_modified=datetime.fromisoformat("2026-05-14T01:00:00+00:00"),
        allowed_groups=[f"space:{space_key}"],
        allowed_users=[],
        webui_link=f"/wiki/{page_id}",
        attachments=attachments or [],
    )


class _FakeSource(DocumentSourceAdapter):
    """미리 만든 PageObject 를 그대로 yield 하는 fake 공급원."""

    def __init__(self, pages: list[PageObject]) -> None:
        self._pages = pages

    def fetch_pages(self, since: datetime | None = None) -> Iterator[PageObject]:
        yield from self._pages

    def list_active_ids(self) -> ActiveIds:
        return ActiveIds()

    def watch_changes(self) -> Iterator[ChangeEvent]:
        yield from ()


def test_run_full_crawl_persists_pages_and_publishes_chunking_messages() -> None:
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    source = _FakeSource([_page("page-1", version=2), _page("page-2", version=5)])

    result = run_full_crawl(
        CrawlRequest(space_key="ENG"),
        raw_store=store,
        publisher=publisher,
        adapter=source,
    )

    assert result.pages_collected == 2
    assert result.attachments_collected == 0
    assert result.failed_page_ids == []
    assert set(store.pages) == {"page-1", "page-2"}

    assert [m.routing_key for m in publisher.messages] == [QUEUE_CHUNKING, QUEUE_CHUNKING]
    first = publisher.messages[0].body
    assert first == {
        "page_id": "page-1",
        "space_key": "ENG",
        "version_number": 2,
        "source_type": "page",
    }


def test_run_full_crawl_filters_by_requested_space_key() -> None:
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    source = _FakeSource([_page("page-1", space_key="ENG"), _page("page-2", space_key="OPS")])

    result = run_full_crawl(
        CrawlRequest(space_key="ENG"),
        raw_store=store,
        publisher=publisher,
        adapter=source,
    )

    assert result.pages_collected == 1
    assert set(store.pages) == {"page-1"}
    assert len(publisher.messages) == 1


def test_run_full_crawl_isolates_failed_page() -> None:
    class _RaisingStore(FakeRawPageStore):
        def save_page(self, page: PageObject) -> None:
            if page.page_id == "page-bad":
                raise RuntimeError("mongo down")
            super().save_page(page)

    store = _RaisingStore()
    publisher = FakeQueuePublisher()
    source = _FakeSource([_page("page-ok"), _page("page-bad")])

    result = run_full_crawl(
        CrawlRequest(space_key="ENG"),
        raw_store=store,
        publisher=publisher,
        adapter=source,
    )

    assert result.pages_collected == 1
    assert result.failed_page_ids == ["page-bad"]
    assert set(store.pages) == {"page-ok"}


def test_run_full_crawl_records_crawl_jobs_when_jobs_injected() -> None:
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    jobs = FakeIngestionJobsRepository()
    source = _FakeSource([_page("page-1"), _page("page-2")])

    run_full_crawl(
        CrawlRequest(space_key="ENG"),
        raw_store=store,
        publisher=publisher,
        adapter=source,
        jobs=jobs,
    )

    assert [r.page_id for r in jobs.records] == ["page-1", "page-2"]
    assert all(r.stage is IngestionStage.CRAWL for r in jobs.records)
    assert all(r.status is IngestionStatus.SUCCESS for r in jobs.records)
    assert all(r.attachment_id is None and r.error is None for r in jobs.records)


def test_run_full_crawl_does_not_record_failed_pages() -> None:
    class _RaisingStore(FakeRawPageStore):
        def save_page(self, page: PageObject) -> None:
            if page.page_id == "page-bad":
                raise RuntimeError("mongo down")
            super().save_page(page)

    store = _RaisingStore()
    publisher = FakeQueuePublisher()
    jobs = FakeIngestionJobsRepository()
    source = _FakeSource([_page("page-ok"), _page("page-bad")])

    result = run_full_crawl(
        CrawlRequest(space_key="ENG"),
        raw_store=store,
        publisher=publisher,
        adapter=source,
        jobs=jobs,
    )

    # 실패 페이지는 잡 레코드를 남기지 않는다(성공 페이지만 CRAWL 기록).
    assert result.failed_page_ids == ["page-bad"]
    assert [r.page_id for r in jobs.records] == ["page-ok"]


# --- 첨부 수집·발행 (featureI-3b) ---


def test_run_full_crawl_persists_attachments_and_publishes_attachment_messages() -> None:
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    page = _page("page-1", version=2, attachments=[_attachment("att-1", page_id="page-1")])
    source = _FakeSource([page])

    result = run_full_crawl(
        CrawlRequest(space_key="ENG"),
        raw_store=store,
        publisher=publisher,
        adapter=source,
    )

    assert result.pages_collected == 1
    assert result.attachments_collected == 1
    assert result.failed_attachment_ids == []
    assert set(store.attachments) == {"att-1"}

    # 본문 메시지 + 첨부 메시지가 같은 content.chunking 큐로 발행된다.
    assert [m.routing_key for m in publisher.messages] == [QUEUE_CHUNKING, QUEUE_CHUNKING]
    assert publisher.messages[1].body == {
        "page_id": "page-1",
        "attachment_id": "att-1",
        "space_key": "ENG",
        "version_number": 2,
        "source_type": "attachment",
    }


def test_run_full_crawl_records_crawl_jobs_for_attachments() -> None:
    store = FakeRawPageStore()
    publisher = FakeQueuePublisher()
    jobs = FakeIngestionJobsRepository()
    page = _page("page-1", attachments=[_attachment("att-1", page_id="page-1")])
    source = _FakeSource([page])

    run_full_crawl(
        CrawlRequest(space_key="ENG"),
        raw_store=store,
        publisher=publisher,
        adapter=source,
        jobs=jobs,
    )

    # 페이지 CRAWL 1건 + 첨부 CRAWL 1건(attachment_id 채워짐).
    assert [r.attachment_id for r in jobs.records] == [None, "att-1"]
    assert all(r.stage is IngestionStage.CRAWL for r in jobs.records)
    assert all(r.status is IngestionStatus.SUCCESS for r in jobs.records)


def test_run_full_crawl_isolates_failed_attachment() -> None:
    class _RaisingStore(FakeRawPageStore):
        def save_attachment(self, attachment: Attachment) -> None:
            if attachment.attachment_id == "att-bad":
                raise RuntimeError("mongo down")
            super().save_attachment(attachment)

    store = _RaisingStore()
    publisher = FakeQueuePublisher()
    page = _page(
        "page-1",
        attachments=[
            _attachment("att-ok", page_id="page-1"),
            _attachment("att-bad", page_id="page-1"),
        ],
    )
    source = _FakeSource([page])

    result = run_full_crawl(
        CrawlRequest(space_key="ENG"),
        raw_store=store,
        publisher=publisher,
        adapter=source,
    )

    # 첨부 실패는 페이지·다른 첨부로 전파하지 않는다(graceful degrade).
    assert result.pages_collected == 1
    assert result.attachments_collected == 1
    assert result.failed_attachment_ids == ["att-bad"]
    assert set(store.attachments) == {"att-ok"}
