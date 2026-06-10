"""Confluence 삭제 Webhook 라우트 회귀 — POST /ml/confluence/webhook (featureI-5b).

parse_confluence_delete_event(이벤트 유형별 파싱·비삭제 무시)와 라우트 end-to-end(soft-delete
반영·ignored·잘못된 JSON 400)를 검증한다. store 는 실 FakeQdrantPoolStore 로 is_deleted 까지 확인.
코드 리뷰 재점검(A18) 후속 — 옵션 공유 시크릿(``webhook_shared_secret``) 설정 시 X-Webhook-Secret
헤더 검증(불일치/누락 401, 일치 200)도 검증한다(get_settings 는 lru_cache 라 env 주입 후
cache_clear 로 반영하고 종료 시 원복한다).
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from httpx import ASGITransport

from app.api.deps import IngestDeps
from app.api.main import create_app
from app.api.routes import get_deps
from app.api.webhook_routes import parse_confluence_delete_event
from app.config import get_settings
from app.ingestion.crawler import CrawlRequest, CrawlResult
from app.ingestion.embedder.base import SparseVector
from app.ingestion.vector_store import CONTENT_POOL
from app.ingestion.workers.sync_worker import SyncWorker, SyncWorkerDeps
from app.schemas.chunk import Chunk, ChunkMetadata
from app.schemas.enums import DocType, SourceType
from app.storage.ingest_jobs import InMemoryIngestJobStore
from app.storage.qdrant_fake import FakeQdrantPoolStore

_EMPTY_SPARSE = SparseVector(indices=(), values=())


# --------------------------- 순수 파서 ---------------------------


def test_parse_page_removed_event() -> None:
    event = parse_confluence_delete_event({"event": "page_removed", "page": {"id": "P1"}})
    assert event is not None
    assert event.page_id == "P1"
    assert event.attachment_id is None


def test_parse_attachment_removed_event() -> None:
    event = parse_confluence_delete_event(
        {"event": "attachment_removed", "attachment": {"id": "ATT-1"}}
    )
    assert event is not None
    assert event.attachment_id == "ATT-1"
    assert event.page_id is None


def test_parse_page_trashed_with_content_object() -> None:
    event = parse_confluence_delete_event({"eventType": "page_trashed", "content": {"id": "P9"}})
    assert event is not None
    assert event.page_id == "P9"


def test_parse_attachment_event_top_level_id() -> None:
    event = parse_confluence_delete_event({"event": "attachment_trashed", "id": "ATT-9"})
    assert event is not None
    assert event.attachment_id == "ATT-9"


def test_parse_non_delete_event_is_ignored() -> None:
    assert parse_confluence_delete_event({"event": "page_created", "page": {"id": "P1"}}) is None


def test_parse_missing_event_or_id_is_none() -> None:
    assert parse_confluence_delete_event({"page": {"id": "P1"}}) is None  # event 없음
    assert parse_confluence_delete_event({"event": "page_removed"}) is None  # id 없음
    assert parse_confluence_delete_event("nope") is None  # 비-dict


# --------------------------- 라우트 ---------------------------


def _chunk(*, chunk_id: str, page_id: str, attachment_id: str | None = None) -> Chunk:
    metadata = ChunkMetadata(
        chunk_id=chunk_id,
        page_id=page_id,
        page_title="T",
        section_header="H",
        section_path="H",
        chunk_index=0,
        labels=[],
        doc_type=DocType.OPERATION,
        space_key="CLOUD",
        allowed_groups=["space:CLOUD"],
        allowed_users=[],
        webui_link="/x",
        last_modified=datetime.fromisoformat("2026-05-14T01:00:00+00:00"),
        source_type=SourceType.PAGE if attachment_id is None else SourceType.ATTACHMENT,
        attachment_id=attachment_id,
        token_count=10,
    )
    return Chunk(text="body", metadata=metadata)


def _deps_with_seeded_store() -> tuple[IngestDeps, FakeQdrantPoolStore]:
    store = FakeQdrantPoolStore()
    store.upsert_chunks_batch(
        CONTENT_POOL, [(_chunk(chunk_id="a" * 40, page_id="P1"), 1, [], _EMPTY_SPARSE)]
    )

    def _run_crawl(request: CrawlRequest) -> CrawlResult:
        return CrawlResult(space_key=request.space_key, pages_collected=0, failed_page_ids=[])

    deps = IngestDeps(
        job_store=InMemoryIngestJobStore(),
        run_crawl=_run_crawl,
        sync_worker=SyncWorker(SyncWorkerDeps(store=store)),
    )
    return deps, store


def _client(deps: IngestDeps) -> httpx.AsyncClient:
    app = create_app()
    app.dependency_overrides[get_deps] = lambda: deps
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.asyncio
async def test_webhook_page_removed_soft_deletes() -> None:
    deps, store = _deps_with_seeded_store()
    async with _client(deps) as client:
        resp = await client.post(
            "/ml/confluence/webhook",
            json={"event": "page_removed", "page": {"id": "P1"}},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ignored"] is False
    assert body["softDeleted"]["pageIds"] == ["P1"]
    # 실제 store Point 의 is_deleted 가 set 됐는지 확인(funnel→store 정합).
    assert store.points[CONTENT_POOL]["a" * 40].is_deleted is True


@pytest.mark.asyncio
async def test_webhook_non_delete_event_ignored() -> None:
    deps, store = _deps_with_seeded_store()
    async with _client(deps) as client:
        resp = await client.post(
            "/ml/confluence/webhook",
            json={"event": "page_created", "page": {"id": "P1"}},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ignored"] is True
    assert body["softDeleted"] == {"pageIds": [], "attachmentIds": []}
    assert store.points[CONTENT_POOL]["a" * 40].is_deleted is False


@pytest.mark.asyncio
async def test_webhook_invalid_json_returns_400_envelope() -> None:
    deps, _store = _deps_with_seeded_store()
    async with _client(deps) as client:
        resp = await client.post(
            "/ml/confluence/webhook",
            content=b"not-json",
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["isSuccess"] is False
    assert body["errorCode"] == "INVALID_REQUEST"


# --------------------------- 공유 시크릿 검증 (A18) ---------------------------

_DELETE_PAYLOAD = {"event": "page_removed", "page": {"id": "P1"}}


@pytest.mark.asyncio
async def test_webhook_rejects_missing_or_wrong_secret_when_configured(monkeypatch) -> None:
    """A18 — 시크릿 설정 시 X-Webhook-Secret 누락/불일치 요청은 401 로 거부(soft-delete 미적용)."""
    monkeypatch.setenv("RAG_WEBHOOK_SHARED_SECRET", "topsecret")
    get_settings.cache_clear()  # lru_cache 무효화 — 라우트가 주입 env 를 읽게 한다.
    try:
        deps, store = _deps_with_seeded_store()
        async with _client(deps) as client:
            missing = await client.post("/ml/confluence/webhook", json=_DELETE_PAYLOAD)
            wrong = await client.post(
                "/ml/confluence/webhook",
                json=_DELETE_PAYLOAD,
                headers={"X-Webhook-Secret": "wrong-secret"},
            )

        for resp in (missing, wrong):
            assert resp.status_code == 401
            body = resp.json()
            assert body == {
                "isSuccess": False,
                "code": 401,
                "errorCode": "UNAUTHORIZED",
                "message": "webhook 시크릿이 일치하지 않습니다",
            }
        # 거부된 요청은 soft-delete 를 수행하지 않는다(임의 대량 soft-delete 방어).
        assert store.points[CONTENT_POOL]["a" * 40].is_deleted is False
    finally:
        get_settings.cache_clear()  # 원복 — 다음 테스트가 기본(빈 시크릿) 설정을 보게 한다.


@pytest.mark.asyncio
async def test_webhook_accepts_matching_secret_when_configured(monkeypatch) -> None:
    """A18 — 시크릿 설정 시 올바른 X-Webhook-Secret 헤더 요청은 기존대로 처리된다(200)."""
    monkeypatch.setenv("RAG_WEBHOOK_SHARED_SECRET", "topsecret")
    get_settings.cache_clear()
    try:
        deps, store = _deps_with_seeded_store()
        async with _client(deps) as client:
            resp = await client.post(
                "/ml/confluence/webhook",
                json=_DELETE_PAYLOAD,
                headers={"X-Webhook-Secret": "topsecret"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ignored"] is False
        assert body["softDeleted"]["pageIds"] == ["P1"]
        assert store.points[CONTENT_POOL]["a" * 40].is_deleted is True
    finally:
        get_settings.cache_clear()
