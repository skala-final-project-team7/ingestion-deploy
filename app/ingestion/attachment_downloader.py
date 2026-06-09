"""첨부 다운로더 — ``download_url`` → ``local_path`` 헬퍼 [Storage 경계].

--------------------------------------------------
작성자 : 최태성
작성목적 : 청커(``chunk_attachment``)는 첨부 파일을 파일 시스템에서 직접 읽으므로 ``local_path`` 가
          채워져 있어야 한다(``app/schemas/page_object.py`` Attachment 주석 — "다운로드 헬퍼가
          local_path 를 채우는 것이 정공법"). 본 모듈은 ``local_path`` 가 없는 첨부를
          ``download_url`` 에서 받아 로컬에 저장하고 ``local_path`` 를 채운 Attachment 를 돌려준다.
          fixture(JsonFixtureSourceAdapter)는 이미 ``local_path`` 를 채우므로 다운로드가 불필요하고,
          운영(Atlassian) 경로에서 본 다운로더가 last-mile 을 담당한다(FR-002).
작성일 : 2026-06-09 (FR-002 — 첨부 다운로더 seam)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-06-09, 최초 작성, FR-002 — AttachmentDownloader Protocol + Noop(기본) + Http(httpx,
    주입형 client) + AttachmentDownloadError. chunking_worker 첨부 경로가 chunk_attachment 전에
    ``ensure_local`` 로 local_path 를 채운다.
--------------------------------------------------
[보안] 자격증명(Bearer/Admin Key 헤더)은 본 모듈이 보관하지 않는다 — 호출자가 구성한 httpx client
       에 주입한다(루트 CLAUDE.md). 다운로드 URL/경로는 로그에 남기되 토큰은 남기지 않는다.
[호환성]
  - Python 3.11.x
  - httpx>=0.27 (HttpAttachmentDownloader 가 사용; Protocol/Noop 은 외부 의존성 0)
--------------------------------------------------
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx

from app.schemas.page_object import Attachment


class AttachmentDownloadError(RuntimeError):
    """첨부 다운로드 실패(네트워크/HTTP/IO 오류).

    운영성(재시도 가능) 오류이므로 chunking_worker 는 격리 status 로 삼키지 않고 전파한다 —
    상위 consumer 루프가 nack/retry/DLQ 로 처리한다(``RawPageNotFoundError`` 와 동일 정책).
    """


class AttachmentDownloader(Protocol):
    """첨부 로컬화 seam — ``local_path`` 가 없는 첨부를 다운로드해 채운 Attachment 를 반환한다."""

    def ensure_local(self, attachment: Attachment) -> Attachment:
        """``local_path`` 가 채워진 Attachment 를 반환한다(이미 로컬이면 그대로)."""
        ...


def _local_path_from_file_uri(download_url: str) -> str | None:
    """``file://`` URI 면 로컬 경로를 반환(다운로드 불필요), 아니면 None."""
    if download_url.startswith("file://"):
        return urlparse(download_url).path
    return None


class NoopAttachmentDownloader:
    """다운로드하지 않는 기본 구현 — fixture(local_path 보유)·PoC 안전.

    ``local_path`` 가 없고 ``download_url`` 이 원격이면 그대로 둔다(청커가 처리 불가 시 첨부 단위
    격리). 운영 다운로드 wiring 은 infra/worker 진입점에서 ``HttpAttachmentDownloader`` 로 교체한다.
    """

    def ensure_local(self, attachment: Attachment) -> Attachment:
        return attachment


class HttpAttachmentDownloader:
    """download_url 바이너리를 httpx 로 받아 download_dir 에 저장하고 local_path 를 채운다.

    인증 헤더(Bearer + ``Atl-Confluence-With-Admin-Key`` 등)는 주입된 ``client`` 에 구성한다 —
    본 다운로더는 자격증명을 보관하지 않는다. 이미 로컬(``local_path`` 또는 ``file://``)이면
    네트워크 호출 없이 그대로 사용한다.
    """

    def __init__(self, *, download_dir: str, client: httpx.Client, timeout: float = 30.0) -> None:
        self._download_dir = Path(download_dir)
        self._client = client
        self._timeout = timeout

    def ensure_local(self, attachment: Attachment) -> Attachment:
        if attachment.local_path:
            return attachment
        file_path = _local_path_from_file_uri(attachment.download_url)
        if file_path is not None:
            return attachment.model_copy(update={"local_path": file_path})

        dest = self._download_dir / _safe_filename(attachment)
        try:
            self._download_dir.mkdir(parents=True, exist_ok=True)
            response = self._client.get(attachment.download_url, timeout=self._timeout)
            response.raise_for_status()
            dest.write_bytes(response.content)
        except (httpx.HTTPError, OSError) as exc:
            raise AttachmentDownloadError(
                f"attachment download failed: id={attachment.attachment_id}"
            ) from exc
        return attachment.model_copy(update={"local_path": str(dest)})


def _safe_filename(attachment: Attachment) -> str:
    """``attachment_id`` 기반 안전한 로컬 파일명(경로 분리자 제거 + 원본 확장자 보존)."""
    base = attachment.attachment_id.replace("/", "_").replace("\\", "_").strip() or "attachment"
    suffix = Path(attachment.filename or "").suffix
    return f"{base}{suffix}"
