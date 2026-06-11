from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Sync Agent 실행 설정 스키마 정의.
          cloud_id와 access_token은 외부 주입으로만 받고 안전 직렬화에서 token을 제거한다.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, feature1 config schema 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses 기반
--------------------------------------------------
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class DataSyncConfig:
    """Delta sync job 실행에 필요한 외부 주입 config.

    Args:
        cloud_id: Atlassian Cloud ID. 코드나 fixture에 고정하지 않는다.
        access_token: Confluence API access token. 안전 직렬화에서는 redaction된다.
        output_dir: local output root directory.
        previous_snapshot: 비교 기준 previous snapshot file path.
        request_delay_seconds: API 요청 사이 지연 시간.
        max_retries: retryable 요청의 최대 재시도 횟수.
        timeout_seconds: 외부 API 요청 timeout.
        use_admin_key: Admin Key 경로를 사용할지 여부. True이면 admin API Token Basic 인증과
            site URL을 사용한다(api-spec v2.6.1).

    Raises:
        ValueError: 필수값이 비어 있거나 retry 설정이 유효하지 않은 경우.
    """

    cloud_id: str
    access_token: str = field(repr=False)
    output_dir: Path | str
    previous_snapshot: Path | str
    request_delay_seconds: float = 0.3
    max_retries: int = 3
    timeout_seconds: int = 20
    use_admin_key: bool = False
    site_url: str = ""
    admin_email: str = ""
    admin_api_token: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.output_dir == "":
            raise ValueError("output_dir is required")
        if self.previous_snapshot == "":
            raise ValueError("previous_snapshot is required")
        self.output_dir = Path(self.output_dir)
        self.previous_snapshot = Path(self.previous_snapshot)
        self.validate()

    def validate(self) -> None:
        """필수 config와 retry 설정의 최소 유효성을 검증한다."""
        if not self.cloud_id:
            raise ValueError("cloud_id is required")
        if not self.access_token:
            raise ValueError("access_token is required")
        if self.use_admin_key:
            if not self.site_url:
                raise ValueError("site_url is required when use_admin_key is true")
            if not self.admin_email:
                raise ValueError("admin_email is required when use_admin_key is true")
            if not self.admin_api_token:
                raise ValueError("admin_api_token is required when use_admin_key is true")
        if not str(self.output_dir):
            raise ValueError("output_dir is required")
        if not str(self.previous_snapshot):
            raise ValueError("previous_snapshot is required")
        if self.request_delay_seconds < 0:
            raise ValueError("request_delay_seconds must be greater than or equal to 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be greater than or equal to 0")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")

    def to_safe_dict(self) -> dict[str, Any]:
        """로그와 report에 사용할 수 있는 token redacted dictionary를 반환한다."""
        return {
            "cloud_id": self.cloud_id,
            "access_token": "<redacted>",
            "output_dir": str(self.output_dir),
            "previous_snapshot": str(self.previous_snapshot),
            "request_delay_seconds": self.request_delay_seconds,
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "use_admin_key": self.use_admin_key,
            "site_url": self.site_url,
            "admin_email": self.admin_email,
            "admin_api_token": "<redacted>",
        }
