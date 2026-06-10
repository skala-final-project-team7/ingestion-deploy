"""build_attachment_downloader (FR-002 bootstrap 배선) 테스트.

- json_fixture 소스 → None(다운로드 불필요, fixture 는 local_path 보유).
- atlassian 소스 → HttpAttachmentDownloader(설정 download_dir 사용) + A11 검증 배선
  (allowed_hosts=Atlassian API 호스트, file_uri_allowed_prefix=samples_dir).
헬퍼는 E5/Qdrant 등 실 어댑터 로딩 없이 단독 검증 가능하다(build_chunking_worker_deps 실 branch
전체는 실 의존성 로딩이 필요해 본 단위 테스트가 wiring 핵심만 커버한다).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr

from app.config import Settings
from app.ingestion.attachment_downloader import HttpAttachmentDownloader
from app.ingestion.bootstrap import build_attachment_downloader


def test_build_attachment_downloader_none_for_fixture_source() -> None:
    assert build_attachment_downloader(Settings(source_type="json_fixture")) is None


def test_build_attachment_downloader_http_for_atlassian_source() -> None:
    settings = Settings(
        source_type="atlassian",
        atlassian_access_token=SecretStr("tok"),
        atlassian_use_admin_key=True,
        attachment_download_dir="/tmp/att",
    )

    downloader = build_attachment_downloader(settings)

    assert isinstance(downloader, HttpAttachmentDownloader)
    assert downloader._download_dir == Path("/tmp/att")
    # A11 — 자격증명이 실린 다운로드는 Atlassian API 호스트로만 허용한다(기본 base_url 호스트).
    assert downloader._allowed_hosts == {"api.atlassian.com"}
    # A11 — file:// URI 는 픽스처 디렉터리(samples_dir) 아래로만 승격한다.
    assert downloader._file_uri_allowed_prefix == settings.samples_dir


def test_build_attachment_downloader_allowlist_follows_configured_base_url() -> None:
    settings = Settings(
        source_type="atlassian",
        atlassian_access_token=SecretStr("tok"),
        atlassian_api_base_url="https://tenant.example.net",
        samples_dir="/srv/fixtures",
    )

    downloader = build_attachment_downloader(settings)

    assert isinstance(downloader, HttpAttachmentDownloader)
    assert downloader._allowed_hosts == {"tenant.example.net"}
    assert downloader._file_uri_allowed_prefix == "/srv/fixtures"
