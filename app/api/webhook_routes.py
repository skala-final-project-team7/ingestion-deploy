"""Confluence 삭제 Webhook 라우트 — POST /ml/confluence/webhook [Pipeline].

--------------------------------------------------
작성자 : 최태성
담당 영역 : ingestion
작성목적 : 3중 삭제 동기화의 즉시(실시간) 경로. Confluence 가 보내는 page/attachment 삭제·
          휴지통 이벤트를 수신해 ``SyncWorker.handle_webhook_event`` 로 Qdrant soft-delete
          (``is_deleted=true``)를 즉시 반영한다. 1차 인증·서명 검증은 BFF/NetworkPolicy 책임
          (api-spec NOTE)이나, 직접 노출 환경 대비 옵션 공유 시크릿 검증을 제공한다(A18).
          인식된 삭제 이벤트가 아니거나 대상 id 가 없으면 200 + ``ignored=true`` 로 응답해
          재시도를 막는다.
작성일 : 2026-06-04 (featureI-5b — 3중 삭제 트리거 배선)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-06-04, 최초 작성, featureI-5b — parse_confluence_delete_event(순수 파서) +
    POST /ml/confluence/webhook 라우트(파싱 → sync_worker.handle_webhook_event → unwrapped data).
  - 2026-06-10, 코드 리뷰 재점검(A18) — 옵션 공유 시크릿 검증 추가. 본 라우트는 본문 id 를
    즉시 soft-delete 하므로 무인증 직접 노출 시 임의 페이지 대량 soft-delete 가 가능하다.
    ``Settings.webhook_shared_secret`` 설정 시 ``X-Webhook-Secret`` 헤더 불일치 요청을 401 로
    거부한다(빈 값 기본 = 기존 동작 보존 — BFF 전단 보호 전제).
--------------------------------------------------
[보안] 페이로드 전체를 로깅하지 않는다(토큰·민감정보 혼입 방지 — 루트 CLAUDE.md 보안 규칙).
[호환성]
  - Python 3.11.x, FastAPI 0.111+
--------------------------------------------------
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.routes import IngestDepsDep
from app.config import get_settings
from app.ingestion.workers.sync_worker import WebhookDeleteEvent

_LOGGER = logging.getLogger(__name__)

webhook_router = APIRouter()

# 처리 대상 Confluence 삭제/휴지통 이벤트. 그 외 이벤트(생성·수정 등)는 무시한다 — 비삭제
# webhook 으로 데이터를 지우지 않는다(보수적 안전 정책).
_DELETE_EVENTS: frozenset[str] = frozenset(
    {
        "page_removed",
        "page_trashed",
        "blogpost_removed",
        "blogpost_trashed",
        "attachment_removed",
        "attachment_trashed",
        "content_removed",
    }
)

# 페이로드에서 page 객체를 담는 후보 키(Confluence 변형 대응).
_PAGE_KEYS: tuple[str, ...] = ("page", "blogpost", "blogPost", "content")


def parse_confluence_delete_event(payload: Any) -> WebhookDeleteEvent | None:
    """Confluence webhook 페이로드에서 삭제 대상(page/attachment) id 를 추출한다(순수 함수).

    인식된 삭제 이벤트(``_DELETE_EVENTS``)만 처리하고, 그 외 이벤트·형식 오류·id 부재는
    ``None`` 을 반환한다(no-op). attachment 이벤트는 attachment_id, 그 외는 page_id 로 매핑한다.

    Args:
        payload: webhook JSON 본문(dict 기대 — 그 외 타입은 None).

    Returns:
        삭제 대상 ``WebhookDeleteEvent`` 또는 처리 대상 아님이면 ``None``.
    """
    if not isinstance(payload, dict):
        return None
    event = str(payload.get("event") or payload.get("eventType") or "").strip().lower()
    if event not in _DELETE_EVENTS:
        return None

    # 1) attachment 객체의 id
    attachment = payload.get("attachment")
    if isinstance(attachment, dict):
        attachment_id = str(attachment.get("id") or "").strip()
        if attachment_id:
            return WebhookDeleteEvent(attachment_id=attachment_id)

    # 2) page/blogpost/content 객체의 id
    for key in _PAGE_KEYS:
        obj = payload.get(key)
        if isinstance(obj, dict):
            page_id = str(obj.get("id") or "").strip()
            if page_id:
                return WebhookDeleteEvent(page_id=page_id)

    # 3) top-level id 폴백 — 이벤트 유형으로 page/attachment 분류
    top_id = str(payload.get("id") or "").strip()
    if top_id:
        if "attachment" in event:
            return WebhookDeleteEvent(attachment_id=top_id)
        return WebhookDeleteEvent(page_id=top_id)
    return None


def _verify_webhook_secret(request: Request) -> bool:
    """옵션 공유 시크릿 검증(A18) — 미설정(빈 값)이면 항상 통과(BFF 전단 보호 전제).

    설정 시 ``X-Webhook-Secret`` 헤더와 상수 시간 비교(``hmac.compare_digest``)한다.
    """
    secret = get_settings().webhook_shared_secret.get_secret_value()
    if not secret:
        return True
    provided = request.headers.get("X-Webhook-Secret", "")
    return hmac.compare_digest(provided, secret)


@webhook_router.post("/ml/confluence/webhook")
async def confluence_webhook_route(request: Request, deps: IngestDepsDep) -> Any:
    """Confluence 실시간 삭제 Webhook 수신 → soft-delete (FR-005 3중 삭제 즉시 경로).

    삭제 이벤트가 아니거나 대상 id 가 없으면 200 + ``ignored=true`` 로 응답한다(Confluence
    재시도 방지). soft-delete 실패는 funnel 이 id 단위로 격리하므로 라우트는 200 으로 결과를
    보고한다. 잘못된 JSON 본문만 4필드 에러 봉투로 400 응답한다. 공유 시크릿이 설정된 경우
    헤더 불일치 요청은 401 로 거부한다(A18 — 임의 soft-delete 방어).
    """
    if not _verify_webhook_secret(request):
        return JSONResponse(
            status_code=401,
            content={
                "isSuccess": False,
                "code": 401,
                "errorCode": "UNAUTHORIZED",
                "message": "webhook 시크릿이 일치하지 않습니다",
            },
        )
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — 잘못된 JSON 은 400 으로 변환(서버 오류 아님)
        return JSONResponse(
            status_code=400,
            content={
                "isSuccess": False,
                "code": 400,
                "errorCode": "INVALID_REQUEST",
                "message": "유효한 JSON 본문이 아닙니다",
            },
        )

    event = parse_confluence_delete_event(payload)
    if event is None or event.is_empty:
        return {"softDeleted": {"pageIds": [], "attachmentIds": []}, "ignored": True}

    result = deps.sync_worker.handle_webhook_event(event)
    return {
        "softDeleted": {
            "pageIds": result.soft_deleted_page_ids,
            "attachmentIds": result.soft_deleted_attachment_ids,
        },
        "ignored": False,
    }
