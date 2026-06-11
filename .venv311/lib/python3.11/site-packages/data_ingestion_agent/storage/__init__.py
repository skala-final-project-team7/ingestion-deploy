"""Local storage repository package."""

from data_ingestion_agent.storage.local_repository import (
    LocalFileRepository,
    LocalWriteResult,
)

__all__ = ["LocalFileRepository", "LocalWriteResult"]
