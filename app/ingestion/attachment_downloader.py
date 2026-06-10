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
  - 2026-06-10, 코드 리뷰 재점검(A11·A12) — (1) ``allowed_hosts`` 호스트 allowlist:
    저장 데이터의 download_url 이 임의 호스트로 자격증명(Bearer/Admin Key)을 보내는
    경로 차단. (2) ``file://`` 는 ``file_uri_allowed_prefix`` 아래만 허용(임의 로컬 파일
    색인 유입 차단). (3) redirect 추적(``follow_redirects=True`` — httpx 가 cross-origin
    redirect 시 Authorization 자동 제거) + 제한 재시도(transient 오류). (4) docstring 의
    "상위 루프 nack/retry/DLQ" 전제 제거 — 실제 처리자는 chunking_worker 의 첨부 단위
    격리(ATTACH_DOWNLOAD_FAILED)다.
--------------------------------------------------
[보안] 자격증명(Bearer/Admin Key 헤더)은 본 모듈이 보관하지 않는다 — 호출자가 구성한 httpx client
       에 주입한다(루트 CLAUDE.md). 다운로드 URL/경로는 로그에 남기되 토큰은 남기지 않는다.
       ``allowed_hosts`` 가 주어지면 그 외 호스트로는 요청 자체를 보내지 않는다(SSRF/유출 방어).
[호환성]
  - Python 3.11.x
  - httpx>=0.27 (HttpAttachmentDownloader 가 사용; Protocol/Noop 은 외부 의존성 0)
--------------------------------------------------
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx

from app.schemas.page_object import Attachment

_LOGGER = logging.getLogger(__name__)


class AttachmentDownloadError(RuntimeError):
    """첨부 다운로드 실패(네트워크/HTTP/IO 오류 또는 URL 검증 거부).

    다운로더 내부의 제한 재시도(transient 오류)를 소진한 뒤에도 실패한 경우다.
    chunking_worker 는 본 예외를 첨부 단위로 격리해 ``ATTACH_DOWNLOAD_FAILED`` 로
    기록한다(코드 리뷰 A4 — consumer 에 nack/DLQ 가 없어 전파 시 poison-message 루프).
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
    본 다운로더는 자격증명을 보관하지 않는다. 이미 로컬(``local_path``)이면 네트워크 호출
    없이 그대로 사용한다.

    URL 검증(코드 리뷰 A11) — ``download_url`` 은 저장 데이터(raw_attachments)에서 오므로
    신뢰 경계 밖이다. ``allowed_hosts`` 가 주어지면 그 외 호스트로는 자격증명이 실린 요청을
    보내지 않고 즉시 실패시킨다. ``file://`` URI 는 ``file_uri_allowed_prefix`` 아래의 경로만
    승격한다(임의 로컬 파일이 색인으로 유입되는 것을 차단). prefix 미설정 시 ``file://`` 거부.

    재시도(코드 리뷰 A12) — Confluence Cloud 첨부 다운로드는 redirect(302) 를 반환할 수 있어
    ``follow_redirects=True`` 로 추적한다(httpx 는 cross-origin redirect 시 Authorization 헤더를
    자동 제거한다). transient 오류(네트워크/5xx)는 ``max_attempts`` 회까지 짧은 backoff 로
    재시도하고, 4xx 는 영구 실패로 즉시 중단한다.
    """

    def __init__(
        self,
        *,
        download_dir: str,
        client: httpx.Client,
        timeout: float = 30.0,
        allowed_hosts: Sequence[str] | None = None,
        file_uri_allowed_prefix: str | None = None,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        self._download_dir = Path(download_dir)
        self._client = client
        self._timeout = timeout
        self._allowed_hosts = {h.lower() for h in allowed_hosts} if allowed_hosts else None
        self._file_uri_allowed_prefix = file_uri_allowed_prefix
        self._max_attempts = max(1, max_attempts)
        self._retry_backoff_seconds = retry_backoff_seconds

    def ensure_local(self, attachment: Attachment) -> Attachment:
        if attachment.local_path:
            return attachment
        file_path = _local_path_from_file_uri(attachment.download_url)
        if file_path is not None:
            return attachment.model_copy(
                update={"local_path": self._validated_file_path(attachment, file_path)}
            )

        self._validate_remote_host(attachment)
        dest = self._download_dir / _safe_filename(attachment)
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                self._download_dir.mkdir(parents=True, exist_ok=True)
                response = self._client.get(
                    attachment.download_url,
                    timeout=self._timeout,
                    follow_redirects=True,
                )
                response.raise_for_status()
                dest.write_bytes(response.content)
                return attachment.model_copy(update={"local_path": str(dest)})
            except httpx.HTTPStatusError as exc:
                # 4xx 는 영구 실패(권한/삭제) — 재시도 무의미, 즉시 중단.
                if exc.response.status_code < 500:
                    raise AttachmentDownloadError(
                        f"attachment download failed (HTTP {exc.response.status_code}): "
                        f"id={attachment.attachment_id}"
                    ) from exc
                last_error = exc
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
            if attempt < self._max_attempts:
                _LOGGER.warning(
                    "attachment download retry %d/%d: id=%s (%s)",
                    attempt,
                    self._max_attempts,
                    attachment.attachment_id,
                    type(last_error).__name__,
                )
                time.sleep(self._retry_backoff_seconds * attempt)
        raise AttachmentDownloadError(
            f"attachment download failed after {self._max_attempts} attempts: "
            f"id={attachment.attachment_id}"
        ) from last_error

    def _validate_remote_host(self, attachment: Attachment) -> None:
        """allowlist 검증 — 자격증명이 실린 요청이 임의 호스트로 나가는 것을 차단(A11)."""
        if self._allowed_hosts is None:
            return
        host = (urlparse(attachment.download_url).hostname or "").lower()
        if host not in self._allowed_hosts:
            raise AttachmentDownloadError(
                f"attachment download refused (host not allowed): "
                f"id={attachment.attachment_id} host={host or '<empty>'}"
            )

    def _validated_file_path(self, attachment: Attachment, file_path: str) -> str:
        """``file://`` 경로를 허용 prefix 아래로 제한한다(A11 — 임의 로컬 파일 유입 차단)."""
        if self._file_uri_allowed_prefix is None:
            raise AttachmentDownloadError(
                f"attachment file:// URI refused (no allowed prefix configured): "
                f"id={attachment.attachment_id}"
            )
        allowed_root = Path(self._file_uri_allowed_prefix).resolve()
        resolved = Path(file_path).resolve()
        if not resolved.is_relative_to(allowed_root):
            raise AttachmentDownloadError(
                f"attachment file:// URI refused (outside allowed prefix): "
                f"id={attachment.attachment_id}"
            )
        return str(resolved)


def _safe_filename(attachment: Attachment) -> str:
    """``attachment_id`` 기반 안전한 로컬 파일명(경로 분리자 제거 + 원본 확장자 보존)."""
    base = attachment.attachment_id.replace("/", "_").replace("\\", "_").strip() or "attachment"
    suffix = Path(attachment.filename or "").suffix
    return f"{base}{suffix}"
