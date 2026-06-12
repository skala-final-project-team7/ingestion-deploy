"""FakeRawPageStore 단위 테스트 — get_page / get_attachment 읽기 경로 (featureI-3b).

작성자 : 최태성
담당 영역 : ingestion

Mongo 경로는 인프라 의존이라 통합 환경에서 검증한다. 본 테스트는 외부 의존성 0 인
FakeRawPageStore 의 멱등 적재 + 식별자 조회 계약만 검증한다(루트 CLAUDE.md 테스트 규칙).
"""

from __future__ import annotations

from datetime import datetime

from app.schemas.enums import ExtractedFormat
from app.schemas.page_object import Attachment, PageObject
from app.storage.raw_store import FakeRawPageStore


def _page(page_id: str = "page-1") -> PageObject:
    return PageObject(
        page_id=page_id,
        space_key="ENG",
        title="Runbook",
        body_html="<p>body</p>",
        version_number=1,
        last_modified=datetime.fromisoformat("2026-05-14T01:00:00+00:00"),
        allowed_groups=["space:ENG"],
        allowed_users=[],
        webui_link=f"/wiki/{page_id}",
    )


def _attachment(attachment_id: str = "att-1", *, page_id: str = "page-1") -> Attachment:
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


def test_get_attachment_returns_saved_attachment() -> None:
    store = FakeRawPageStore()
    store.save_attachment(_attachment("att-1"))

    loaded = store.get_attachment("att-1")

    assert loaded is not None
    assert loaded.attachment_id == "att-1"
    assert loaded.download_url.endswith("att-1")


def test_get_attachment_missing_returns_none() -> None:
    assert FakeRawPageStore().get_attachment("ghost") is None


def test_save_attachment_is_idempotent_by_attachment_id() -> None:
    store = FakeRawPageStore()
    store.save_attachment(_attachment("att-1", page_id="page-1"))
    # 같은 attachment_id 재적재는 덮어쓴다(멱등).
    store.save_attachment(_attachment("att-1", page_id="page-2"))

    loaded = store.get_attachment("att-1")
    assert loaded is not None
    assert loaded.parent_page_id == "page-2"
    assert set(store.attachments) == {"att-1"}


def test_get_page_and_get_attachment_are_independent() -> None:
    store = FakeRawPageStore()
    store.save_page(_page("page-1"))
    store.save_attachment(_attachment("att-1"))

    assert store.get_page("page-1") is not None
    assert store.get_attachment("att-1") is not None
    # 페이지 키로 첨부를 찾거나 그 반대는 되지 않는다(별도 컬렉션).
    assert store.get_attachment("page-1") is None
    assert store.get_page("att-1") is None
