"""build_attachment_downloader (FR-002 bootstrap 배선) 테스트.

- json_fixture 소스 → None(다운로드 불필요, fixture 는 local_path 보유).
- atlassian 소스 → HttpAttachmentDownloader(설정 download_dir 사용).
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
