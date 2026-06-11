from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Data Sync Agent deleted item 및 message payload 생성 helper 구현.
          실제 RabbitMQ 발행 없이 local payload 구조만 생성한다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature6 deleted/message payload helper 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 dataclasses/json/pathlib 기반
--------------------------------------------------
"""

import json
from pathlib import Path
from typing import Iterable

from data_sync_agent.schemas import (
    ChangedDocument,
    ChangeType,
    DeletedItem,
    MessageEventType,
    MessagePayload,
)
from data_sync_agent.sync.diff_engine import PageChange


def build_deleted_item_from_change(
    page_change: PageChange,
    *,
    sync_id: str,
    detected_at: str,
) -> DeletedItem:
    """deleted_candidate PageChange를 DeletedItem schema로 변환한다."""
    if page_change.change_type != ChangeType.DELETED_CANDIDATE:
        raise ValueError("page_change must be deleted_candidate")
    if page_change.previous is None:
        raise ValueError("page_change.previous is required")
    page = page_change.previous
    return DeletedItem(
        sync_id=sync_id,
        cloud_id=page.cloud_id,
        space_id=page.space_id,
        page_id=page.page_id,
        title=page.title,
        page_url=page.page_url,
        last_seen_version=page.version_number,
        detected_at=detected_at,
        deletion_status="candidate",
        page_key=page.page_key,
    )


def build_changed_message_payload(
    document: ChangedDocument,
    *,
    payload_ref: str,
) -> MessagePayload:
    """ChangedDocument를 후속 chunking/embedding 요청 payload로 변환한다."""
    return MessagePayload(
        sync_id=document.sync_id,
        event_type=MessageEventType.CHUNKING_REQUESTED,
        page_id=str(document.page["page_id"]),
        space_id=str(document.space["space_id"]),
        document_id=document.document_id,
        change_type=document.change_type,
        payload_ref=payload_ref,
        downstream_target="chunking_embedding_pipeline",
    )


def build_deleted_message_payload(
    deleted_item: DeletedItem,
    *,
    payload_ref: str,
) -> MessagePayload:
    """DeletedItem을 vector DB update 단계가 소비할 삭제 후보 payload로 변환한다."""
    return MessagePayload(
        sync_id=deleted_item.sync_id,
        event_type=MessageEventType.DELETE_CANDIDATE_DETECTED,
        page_id=deleted_item.page_id,
        space_id=deleted_item.space_id,
        document_id=None,
        change_type=ChangeType.DELETED_CANDIDATE,
        payload_ref=payload_ref,
        downstream_target="vector_db_update",
    )


def build_message_payloads(
    *,
    changed_documents: list[ChangedDocument],
    deleted_items: list[DeletedItem],
    skipped_changes: list[PageChange],
) -> list[MessagePayload]:
    """changed/deleted 산출물만 message payload로 변환한다.

    skipped_changes는 호출자가 unchanged/failed 항목을 전달해도 payload가 생성되지
    않는다는 계약을 명확히 하기 위한 입력이다.
    """
    _ = skipped_changes
    payloads: list[MessagePayload] = []
    for index, document in enumerate(changed_documents, start=1):
        payloads.append(
            build_changed_message_payload(
                document,
                payload_ref=f"changed/changed_documents.jsonl#{index}",
            )
        )
    for index, deleted_item in enumerate(deleted_items, start=1):
        payloads.append(
            build_deleted_message_payload(
                deleted_item,
                payload_ref=f"deleted/deleted_items.jsonl#{index}",
            )
        )
    return payloads


class LocalMessagePayloadWriter:
    """Message payload JSONL local writer."""

    def __init__(self, output_dir: Path | str) -> None:
        if output_dir == "":
            raise ValueError("output_dir is required")
        self.output_dir = Path(output_dir)

    def write(
        self,
        payloads: Iterable[MessagePayload],
        *,
        output_path: Path | str | None = None,
    ) -> Path:
        """payload 목록을 JSONL로 저장하고 경로를 반환한다."""
        path = Path(output_path) if output_path is not None else self.default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(payload.to_dict(), ensure_ascii=False, sort_keys=True)
            for payload in payloads
        ]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return path

    def default_path(self) -> Path:
        """기본 message payload JSONL 경로를 반환한다."""
        return self.output_dir / "messages" / "message_payloads.jsonl"
