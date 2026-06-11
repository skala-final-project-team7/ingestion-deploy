from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Sync Agent local snapshot repository 구현.
          previous/current snapshot 파일 I/O를 workflow 로직과 분리해 후속 MongoDB adapter 교체를 쉽게 한다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature2 local snapshot repository 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 json/pathlib/dataclasses 기반
--------------------------------------------------
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Protocol

from data_sync_agent.schemas import PageSnapshot, PageSnapshotItem

LOCAL_SNAPSHOT_FORMAT_VERSION = "data-sync-snapshot-v1"
LATEST_SNAPSHOT_FILE_NAME = "latest_snapshot.json"


class SnapshotRepositoryError(ValueError):
    """Snapshot repository I/O 또는 schema validation 실패."""


@dataclass(frozen=True, slots=True)
class SnapshotWriteResult:
    """Snapshot 저장 결과 metadata."""

    path: Path
    format_version: str
    generated_at: str


class SnapshotRepository(Protocol):
    """Snapshot repository adapter contract."""

    def latest_snapshot_path(self) -> Path:
        """현재 repository의 latest snapshot 기본 경로를 반환한다."""

    def load_previous_snapshot(
        self,
        snapshot_path: Path | str,
        *,
        cloud_id: str,
        sync_id: str,
        generated_at: str | None = None,
    ) -> PageSnapshot:
        """previous snapshot을 로드하거나 파일이 없으면 빈 snapshot을 반환한다."""

    def save_current_snapshot(
        self,
        snapshot: PageSnapshot,
        *,
        snapshot_path: Path | str | None = None,
        generated_at: str | None = None,
    ) -> SnapshotWriteResult:
        """current snapshot을 저장하고 저장 결과 metadata를 반환한다."""


class LocalSnapshotRepository:
    """Local JSON file 기반 snapshot repository."""

    def __init__(self, output_dir: Path | str) -> None:
        if output_dir == "":
            raise ValueError("output_dir is required")
        self.output_dir = Path(output_dir)

    def latest_snapshot_path(self) -> Path:
        """`output_dir/snapshots/latest_snapshot.json` 경로를 반환한다."""
        return self.output_dir / "snapshots" / LATEST_SNAPSHOT_FILE_NAME

    def load_previous_snapshot(
        self,
        snapshot_path: Path | str,
        *,
        cloud_id: str,
        sync_id: str,
        generated_at: str | None = None,
    ) -> PageSnapshot:
        """previous snapshot JSON을 PageSnapshot으로 복원한다.

        파일이 없으면 delta sync 최초 실행으로 보고 빈 previous snapshot을 반환한다.
        malformed JSON 또는 schema 불일치는 SnapshotRepositoryError로 감싼다.
        """
        path = Path(snapshot_path)
        if not path.exists():
            return PageSnapshot(
                snapshot_id=f"empty-previous-{sync_id}",
                sync_id=sync_id,
                cloud_id=cloud_id,
                created_at=generated_at or _utc_now_iso(),
                pages=[],
            )

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except JSONDecodeError as exc:
            raise SnapshotRepositoryError(
                f"Malformed snapshot JSON at {path}"
            ) from exc

        try:
            snapshot = _snapshot_from_payload(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise SnapshotRepositoryError(
                f"Invalid snapshot schema at {path}: {exc}"
            ) from exc

        if snapshot.cloud_id != cloud_id:
            raise SnapshotRepositoryError(
                f"Invalid snapshot schema at {path}: cloud_id mismatch"
            )
        return snapshot

    def save_current_snapshot(
        self,
        snapshot: PageSnapshot,
        *,
        snapshot_path: Path | str | None = None,
        generated_at: str | None = None,
    ) -> SnapshotWriteResult:
        """current snapshot을 local JSON envelope로 저장한다."""
        snapshot.validate()
        path = Path(snapshot_path) if snapshot_path is not None else self.latest_snapshot_path()
        resolved_generated_at = generated_at or _utc_now_iso()
        payload = {
            "format_version": LOCAL_SNAPSHOT_FORMAT_VERSION,
            "generated_at": resolved_generated_at,
            "snapshot": snapshot.to_dict(),
        }

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return SnapshotWriteResult(
            path=path,
            format_version=LOCAL_SNAPSHOT_FORMAT_VERSION,
            generated_at=resolved_generated_at,
        )


def _snapshot_from_payload(payload: Any) -> PageSnapshot:
    if not isinstance(payload, dict):
        raise ValueError("snapshot payload must be an object")
    if payload.get("format_version") != LOCAL_SNAPSHOT_FORMAT_VERSION:
        raise ValueError("format_version is invalid")
    if not payload.get("generated_at"):
        raise ValueError("generated_at is required")

    snapshot_payload = payload["snapshot"]
    if not isinstance(snapshot_payload, dict):
        raise ValueError("snapshot must be an object")

    pages_payload = snapshot_payload["pages"]
    if not isinstance(pages_payload, list):
        raise ValueError("snapshot.pages must be a list")

    pages = [_snapshot_item_from_dict(page_payload) for page_payload in pages_payload]
    return PageSnapshot(
        snapshot_id=snapshot_payload["snapshot_id"],
        sync_id=snapshot_payload["sync_id"],
        cloud_id=snapshot_payload["cloud_id"],
        created_at=snapshot_payload["created_at"],
        pages=pages,
    )


def _snapshot_item_from_dict(payload: Any) -> PageSnapshotItem:
    if not isinstance(payload, dict):
        raise ValueError("snapshot page must be an object")
    return PageSnapshotItem(
        page_key=payload.get("page_key"),
        cloud_id=payload["cloud_id"],
        space_id=payload["space_id"],
        space_key=payload["space_key"],
        space_name=payload["space_name"],
        page_id=payload["page_id"],
        title=payload["title"],
        status=payload["status"],
        page_url=payload["page_url"],
        last_modified_at=payload["last_modified_at"],
        version_number=int(payload["version_number"]),
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
