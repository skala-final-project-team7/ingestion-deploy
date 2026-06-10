"""첨부 다운로더 + chunking_worker 배선 테스트 (FR-002 + 코드 리뷰 A11·A12).

- Noop: 무변경(no-op).
- Http: 이미 로컬(local_path)은 네트워크 없이 통과, 원격 download_url 은 fetch→local_path,
  영구 HTTP 오류(4xx)·재시도 소진은 AttachmentDownloadError.
- URL 검증(A11): ``allowed_hosts`` 밖 호스트는 요청 자체를 거부, ``file://`` 는
  ``file_uri_allowed_prefix`` 아래 경로만 승격(미설정 시 거부).
- 재시도(A12): 5xx/transport 오류는 max_attempts 까지 재시도(성공 시 즉시 반환),
  redirect 는 follow_redirects=True 로 추적.
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


def test_http_downloader_file_uri_under_allowed_prefix_resolves_without_fetch(
    tmp_path: Path,
) -> None:
    def _fail(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called for file:// url")

    fixture = tmp_path / "attachments" / "ATT-1.pdf"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"%PDF-1.4 fixture")

    downloader = HttpAttachmentDownloader(
        download_dir=str(tmp_path / "downloads"),
        client=_client(_fail),
        file_uri_allowed_prefix=str(tmp_path),
    )
    att = _attachment(download_url=fixture.as_uri(), local_path=None)
    assert downloader.ensure_local(att).local_path == str(fixture.resolve())


def test_http_downloader_rejects_file_uri_when_no_prefix_configured(tmp_path: Path) -> None:
    """A11 — prefix 미설정(기본)이면 file:// 는 임의 로컬 파일 유입 차단을 위해 거부된다."""

    def _fail(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called for file:// url")

    downloader = HttpAttachmentDownloader(download_dir=str(tmp_path), client=_client(_fail))
    att = _attachment(download_url="file:///etc/passwd", local_path=None)
    with pytest.raises(AttachmentDownloadError, match="no allowed prefix"):
        downloader.ensure_local(att)


def test_http_downloader_rejects_file_uri_outside_allowed_prefix(tmp_path: Path) -> None:
    """A11 — prefix 가 설정돼도 그 밖의 경로(.. 탈출 포함)는 거부된다(resolve 후 비교)."""

    def _fail(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called for file:// url")

    allowed = tmp_path / "samples"
    allowed.mkdir()
    downloader = HttpAttachmentDownloader(
        download_dir=str(tmp_path),
        client=_client(_fail),
        file_uri_allowed_prefix=str(allowed),
    )

    outside = _attachment(download_url=(tmp_path / "secret.pdf").as_uri(), local_path=None)
    with pytest.raises(AttachmentDownloadError, match="outside allowed prefix"):
        downloader.ensure_local(outside)

    # 경로 문자열은 prefix 아래처럼 보여도 ``..`` 로 탈출하면 거부된다.
    escaping = _attachment(
        download_url=f"file://{allowed}/../secret.pdf",
        local_path=None,
    )
    with pytest.raises(AttachmentDownloadError, match="outside allowed prefix"):
        downloader.ensure_local(escaping)


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


def test_http_downloader_5xx_exhausts_retries_then_raises(tmp_path: Path) -> None:
    """A12 — 5xx 는 transient 로 간주해 max_attempts 까지 재시도 후 AttachmentDownloadError."""
    calls: list[httpx.Request] = []

    def _err(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    downloader = HttpAttachmentDownloader(
        download_dir=str(tmp_path), client=_client(_err), retry_backoff_seconds=0
    )
    with pytest.raises(AttachmentDownloadError):
        downloader.ensure_local(_attachment(local_path=None))
    assert len(calls) == 3  # 기본 max_attempts


def test_http_downloader_retries_5xx_then_succeeds(tmp_path: Path) -> None:
    """A12 — transient 5xx 1회 후 성공하면 재시도에서 local_path 가 채워진다."""
    body = b"%PDF-1.4 retried bytes"
    calls: list[httpx.Request] = []

    def _flaky(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=body)

    downloader = HttpAttachmentDownloader(
        download_dir=str(tmp_path), client=_client(_flaky), retry_backoff_seconds=0
    )
    out = downloader.ensure_local(_attachment(local_path=None))

    assert len(calls) == 2  # 실패 1 + 성공 1 — 성공 즉시 반환
    assert out.local_path is not None
    assert Path(out.local_path).read_bytes() == body


def test_http_downloader_4xx_fails_immediately_without_retry(tmp_path: Path) -> None:
    """A12 — 4xx(권한/삭제)는 영구 실패라 재시도 없이 즉시 AttachmentDownloadError."""
    calls: list[httpx.Request] = []

    def _forbidden(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(403)

    downloader = HttpAttachmentDownloader(
        download_dir=str(tmp_path), client=_client(_forbidden), retry_backoff_seconds=0
    )
    with pytest.raises(AttachmentDownloadError, match="HTTP 403"):
        downloader.ensure_local(_attachment(local_path=None))
    assert len(calls) == 1


def test_http_downloader_follows_redirect(tmp_path: Path) -> None:
    """A12 — Confluence Cloud 첨부는 302 를 반환할 수 있어 redirect 를 추적한다."""
    body = b"%PDF-1.4 redirected bytes"

    def _redirect(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("ATT-1.pdf"):
            return httpx.Response(
                302, headers={"Location": "https://media.example/final/ATT-1.bin"}
            )
        return httpx.Response(200, content=body)

    downloader = HttpAttachmentDownloader(download_dir=str(tmp_path), client=_client(_redirect))
    out = downloader.ensure_local(_attachment(local_path=None))

    assert out.local_path is not None
    assert Path(out.local_path).read_bytes() == body


def test_http_downloader_rejects_host_not_in_allowlist(tmp_path: Path) -> None:
    """A11 — allowlist 밖 호스트로는 자격증명이 실린 요청을 보내지 않고 즉시 거부한다."""

    def _fail(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent to a non-allowlisted host")

    downloader = HttpAttachmentDownloader(
        download_dir=str(tmp_path),
        client=_client(_fail),
        allowed_hosts=["api.atlassian.com"],
    )
    att = _attachment(download_url="https://evil.example/wiki/download/ATT-1.pdf")
    with pytest.raises(AttachmentDownloadError, match="host not allowed"):
        downloader.ensure_local(att)


def test_http_downloader_allows_allowlisted_host_case_insensitive(tmp_path: Path) -> None:
    body = b"%PDF-1.4 allowed host bytes"

    def _ok(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    downloader = HttpAttachmentDownloader(
        download_dir=str(tmp_path),
        client=_client(_ok),
        allowed_hosts=["API.Atlassian.com"],
    )
    att = _attachment(download_url="https://api.atlassian.com/ex/confluence/att/ATT-1.pdf")
    out = downloader.ensure_local(att)

    assert out.local_path is not None
    assert Path(out.local_path).read_bytes() == body


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
