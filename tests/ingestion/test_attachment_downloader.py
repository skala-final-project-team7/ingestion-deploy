"""첨부 다운로더 + chunking_worker 배선 테스트 (FR-002).

- Noop: 무변경(no-op).
- Http: 이미 로컬(local_path)·file:// 는 네트워크 없이 통과, 원격 download_url 은 fetch→local_path,
  HTTP 오류는 AttachmentDownloadError.
- 배선: _process_attachment_message 가 chunk_attachment 전에 ensure_local 로 local_path 를 채운다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.ingestion.attachment_downloader import (
    AttachmentDownloadError,
    HttpAttachmentDownloader,
    NoopAttachmentDownloader,
)
from app.ingestion.workers.chunking_worker import ChunkingWorkerDeps, process_chunking_message
from app.schemas.enums import ExtractedFormat, IngestionStatus
from app.schemas.page_object import Attachment, PageObject
from app.storage.raw_store import FakeRawPageStore

# analyze_attachment 통과용 — 길이 ≥200 + 반복 비율 낮은 본문.
_VALID_TEXT = "Valid attachment body with enough length and lexical variety to pass checks. " * 4


def _attachment(
    *,
    attachment_id: str = "ATT-1",
    download_url: str = "https://confluence.example/wiki/download/ATT-1.pdf",
    local_path: str | None = None,
    mime_type: str = "application/pdf",
    filename: str = "ATT-1.pdf",
) -> Attachment:
    return Attachment(
        attachment_id=attachment_id,
        filename=filename,
        mime_type=mime_type,
        extracted_text=_VALID_TEXT,
        extracted_format=ExtractedFormat.RAW_TEXT,
        download_url=download_url,
        parent_page_id="P1",
        last_modified=datetime.fromisoformat("2026-05-14T01:00:00+00:00"),
        local_path=local_path,
    )


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- Noop


def test_noop_downloader_returns_unchanged() -> None:
    att = _attachment(local_path=None)
    assert NoopAttachmentDownloader().ensure_local(att) is att


# ---------------------------------------------------------------- Http


def test_http_downloader_already_local_skips_fetch(tmp_path: Path) -> None:
    def _fail(_request: httpx.Request) -> httpx.Response:  # 호출되면 안 됨
        raise AssertionError("network must not be called when local_path is set")

    downloader = HttpAttachmentDownloader(download_dir=str(tmp_path), client=_client(_fail))
    att = _attachment(local_path="/already/here.pdf")
    assert downloader.ensure_local(att).local_path == "/already/here.pdf"


def test_http_downloader_file_uri_resolves_without_fetch(tmp_path: Path) -> None:
    def _fail(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called for file:// url")

    downloader = HttpAttachmentDownloader(download_dir=str(tmp_path), client=_client(_fail))
    att = _attachment(download_url="file:///samples/attachments/ATT-1.pdf", local_path=None)
    assert downloader.ensure_local(att).local_path == "/samples/attachments/ATT-1.pdf"


def test_http_downloader_fetches_and_sets_local_path(tmp_path: Path) -> None:
    body = b"%PDF-1.4 fake attachment bytes"

    def _ok(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    downloader = HttpAttachmentDownloader(download_dir=str(tmp_path), client=_client(_ok))
    out = downloader.ensure_local(_attachment(local_path=None))

    assert out.local_path is not None
    saved = Path(out.local_path)
    assert saved.parent == tmp_path
    assert saved.read_bytes() == body


def test_http_downloader_http_error_raises_download_error(tmp_path: Path) -> None:
    def _err(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    downloader = HttpAttachmentDownloader(download_dir=str(tmp_path), client=_client(_err))
    with pytest.raises(AttachmentDownloadError):
        downloader.ensure_local(_attachment(local_path=None))


# ---------------------------------------------------------------- chunking_worker 배선


class _RecordingDownloader:
    """ensure_local 호출을 기록하고 local_path 를 채워 돌려주는 테스트용 다운로더."""

    def __init__(self, local_path: str) -> None:
        self._local_path = local_path
        self.calls: list[str] = []

    def ensure_local(self, attachment: Attachment) -> Attachment:
        self.calls.append(attachment.attachment_id)
        return attachment.model_copy(update={"local_path": self._local_path})


def _page() -> PageObject:
    return PageObject(
        page_id="P1",
        space_key="CLOUD",
        title="T",
        body_html="<p>x</p>",
        version_number=1,
        last_modified=datetime.fromisoformat("2026-05-14T01:00:00+00:00"),
        allowed_groups=["space:CLOUD"],
        allowed_users=[],
        webui_link="/wiki/P1",
    )


def test_process_attachment_message_downloads_before_chunking() -> None:
    """배선 — 다운로더 주입 시 chunk_attachment 전에 local_path 가 채워진다(FR-002)."""
    store = FakeRawPageStore()
    store.save_page(_page())
    store.save_attachment(_attachment(local_path=None))

    captured: list[Attachment] = []

    def _capture(attachment: Attachment, page: PageObject, attachment_type: Any) -> list[Any]:
        captured.append(attachment)
        return []  # 빈 청크 → index 단계 없이 SUCCESS 로 종결

    downloader = _RecordingDownloader(local_path="/tmp/ATT-1.pdf")
    deps = ChunkingWorkerDeps(
        raw_store=store,
        dense_embedder=None,
        sparse_embedder=None,
        store=None,
        cache=None,
        attachment_downloader=downloader,
        chunk_attachment_fn=_capture,
    )

    result = process_chunking_message(
        {"source_type": "attachment", "page_id": "P1", "attachment_id": "ATT-1"}, deps
    )

    assert result.status == IngestionStatus.SUCCESS
    assert downloader.calls == ["ATT-1"]
    assert captured and captured[0].local_path == "/tmp/ATT-1.pdf"


def test_process_attachment_message_without_downloader_keeps_attachment() -> None:
    """기본(다운로더 None)은 다운로드 단계를 생략한다(fixture 는 이미 local_path 보유)."""
    store = FakeRawPageStore()
    store.save_page(_page())
    store.save_attachment(_attachment(local_path="/samples/ATT-1.pdf"))

    captured: list[Attachment] = []

    def _capture(attachment: Attachment, page: PageObject, attachment_type: Any) -> list[Any]:
        captured.append(attachment)
        return []

    deps = ChunkingWorkerDeps(
        raw_store=store,
        dense_embedder=None,
        sparse_embedder=None,
        store=None,
        cache=None,
        chunk_attachment_fn=_capture,
    )

    result = process_chunking_message(
        {"source_type": "attachment", "page_id": "P1", "attachment_id": "ATT-1"}, deps
    )

    assert result.status == IngestionStatus.SUCCESS
    assert captured and captured[0].local_path == "/samples/ATT-1.pdf"
