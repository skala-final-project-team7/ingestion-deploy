"""Ingestion job progress callback contracts."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypedDict


class IngestProgress(TypedDict, total=False):
    """Progress payload emitted by full crawl and delta sync internals."""

    phase: str
    total_pages: int
    processed_pages: int
    failed_pages: int


IngestProgressCallback = Callable[[IngestProgress], None]
