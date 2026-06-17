"""Sync log storage adapter tests."""

from __future__ import annotations

from datetime import datetime

from app.storage.sync_logs import FakeSyncLogRepository


def test_fake_sync_log_repository_normalizes_and_upserts_records() -> None:
    repository = FakeSyncLogRepository()

    repository.record(
        {
            "syncId": "sync-001",
            "jobId": "job-001",
            "mode": "delta",
            "status": "COMPLETED",
            "updatedPages": "2",
            "deletedPages": 1,
            "failedPages": 0,
            "startedAt": "2026-06-17T00:00:00Z",
            "completedAt": "2026-06-17T00:00:05Z",
            "duration": "5",
        }
    )
    repository.record(
        {
            "syncId": "sync-001",
            "jobId": "job-001",
            "mode": "delta",
            "status": "FAILED",
            "updatedPages": 0,
            "deletedPages": 0,
            "failedPages": 1,
            "completedAt": "2026-06-17T00:01:00Z",
            "duration": 60,
        }
    )

    assert len(repository.records) == 1
    [record] = repository.records
    assert record["status"] == "FAILED"
    assert record["updatedPages"] == 0
    assert record["failedPages"] == 1
    assert isinstance(record["completedAt"], datetime)
    assert isinstance(record["updatedAt"], datetime)
