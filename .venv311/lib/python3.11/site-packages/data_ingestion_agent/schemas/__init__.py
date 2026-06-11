"""Canonical schemas for Data Ingestion Agent MVP outputs."""

from data_ingestion_agent.schemas.documents import (
    AttachmentProcessingStatus,
    BodyContent,
    PageInfo,
    ProcessedDocument,
    ProcessedDocumentMetadata,
    SpaceInfo,
)
from data_ingestion_agent.schemas.failed_items import (
    FailedItem,
    FailedItemStage,
    FailedItemType,
)
from data_ingestion_agent.schemas.reports import (
    IngestionReport,
    IngestionReportCounts,
    IngestionReportStatus,
)

__all__ = [
    "AttachmentProcessingStatus",
    "BodyContent",
    "FailedItem",
    "FailedItemStage",
    "FailedItemType",
    "IngestionReport",
    "IngestionReportCounts",
    "IngestionReportStatus",
    "PageInfo",
    "ProcessedDocument",
    "ProcessedDocumentMetadata",
    "SpaceInfo",
]
