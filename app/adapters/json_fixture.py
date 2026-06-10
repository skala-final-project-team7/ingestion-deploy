"""JSON 픽스처 기반 Document Source Adapter.

--------------------------------------------------
작성자 : 최태성
작성목적 : samples/*.json(Atlassian-Python-API 응답 포맷)을 읽어 표준 PageObject로
          변환한다. 백엔드/Atlassian 연동 전까지 Ingestion 파이프라인의 입력 소스이자
          단위·통합 테스트의 데이터 소스로 사용한다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature2 — JsonFixtureSourceAdapter + Atlassian 포맷 매핑
  - 2026-05-17, 코드 리뷰 후속(P1-3) — download_url을 file:// URI(사용자 노출용)로 두고,
    청커가 직접 열 로컬 경로는 local_path 필드에 분리 매핑 (ADR-2026-001)
  - 2026-06-10, 코드 리뷰 재점검(P1-6) — 픽스처 파일 부재 시 경로·설정 힌트를 담은
    FileNotFoundError 로 즉시 표면화(기본 ingest 가 원인 불명 FAILED 로 죽던 문제).
  - 2026-06-10, A8 잔여 — raw["space"].id/name → PageObject.space_id/space_name 매핑
    (+P1-3 위임 문구를 rag 레포와 동일하게 정정).
--------------------------------------------------
[호환성]
  - Python 3.11.x, Pydantic 2.7+
--------------------------------------------------
"""

import json
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from app.adapters.base import ActiveIds, ChangeEvent, DocumentSourceAdapter
from app.schemas.enums import ExtractedFormat
from app.schemas.page_object import Attachment, PageObject

_DEFAULT_FIXTURES = ("confluence_sample_data.json", "datadog_docs.json")
# 콜론 없는 타임존 오프셋(예: +0900) → +09:00 정규화용
_OFFSET_NO_COLON = re.compile(r"([+-]\d{2})(\d{2})$")
# extracted_format = sheet_serialized 로 분류할 mime 힌트
_SHEET_MIME_HINTS = ("spreadsheetml", "csv", "excel")


def parse_atlassian_datetime(value: str) -> datetime:
    """Atlassian version.when(ISO 8601) 문자열을 datetime으로 파싱한다.

    Confluence는 '+0900'(콜론 없는 오프셋), 임포트 데이터는 '+00:00'을 사용하므로
    오프셋을 정규화한 뒤 datetime.fromisoformat으로 파싱한다.

    Args:
        value: ISO 8601 일시 문자열 (예: '2026-04-22T08:15:00.000+0900').

    Returns:
        타임존 정보를 가진 datetime.
    """
    normalized = _OFFSET_NO_COLON.sub(r"\1:\2", value)
    return datetime.fromisoformat(normalized)


def infer_extracted_format(mime_type: str) -> ExtractedFormat:
    """첨부 mime_type으로 extracted_format을 추정한다.

    스프레드시트/CSV는 sheet_serialized, 그 외(PDF/Word)는 raw_text로 분류한다.
    """
    lowered = mime_type.lower()
    if any(hint in lowered for hint in _SHEET_MIME_HINTS):
        return ExtractedFormat.SHEET_SERIALIZED
    return ExtractedFormat.RAW_TEXT


class JsonFixtureSourceAdapter(DocumentSourceAdapter):
    """JSON 픽스처 기반 Document Source Adapter — 로컬 개발·테스트용.

    samples/*.json(Atlassian-Python-API 응답 포맷)을 표준 PageObject로 변환한다.

    [ACL 합성 — PoC]
    샘플 데이터에 ACL 필드가 없으므로 space_key 기반으로 allowed_groups를 합성한다.
    실제 ACL 연동 시 ``_synthesize_acl``만 교체한다 (docs/db-schema.md §1.4 미해결 사항).

    [첨부 처리]
    샘플 JSON은 첨부 메타(filename/content_type)만 가진다. 누락 필드는 합성하며,
    청커가 직접 열 실제 경로를 ``local_path`` 에 채운다(ADR-2026-001 — 파일 기반 추출이
    정공법). ``extracted_text`` 는 빈 문자열로 두며, 분석기는 빈 텍스트 + local_path
    조합을 파일 기반 추출(chunk_attachment)로 위임한다(P1-3).
    """

    def __init__(
        self,
        samples_dir: str | Path = "samples",
        fixture_files: list[str] | None = None,
    ) -> None:
        self.samples_dir = Path(samples_dir)
        self.fixture_files = list(fixture_files) if fixture_files else list(_DEFAULT_FIXTURES)

    # --- DocumentSourceAdapter 인터페이스 ---

    def fetch_pages(self, since: datetime | None = None) -> Iterator[PageObject]:
        for raw in self._iter_raw_pages():
            page = self._map_page(raw)
            if since is not None and page.last_modified < since:
                continue
            yield page

    def list_active_ids(self) -> ActiveIds:
        ids = ActiveIds()
        for page in self.fetch_pages():
            ids.pages.add(page.page_id)
            for attachment in page.attachments:
                ids.attachments.add(attachment.attachment_id)
        return ids

    def watch_changes(self) -> Iterator[ChangeEvent]:
        # 정적 JSON 픽스처는 실시간 변경 이벤트가 없다.
        yield from ()

    # --- 내부 헬퍼 ---

    def _iter_raw_pages(self) -> Iterator[dict]:
        """픽스처 파일들의 single_page_responses를 순회한다.

        픽스처 부재는 즉시 명확한 오류로 표면화한다(P1-6) — 종전에는 기본 설정의
        ``POST /ml/ingest`` 가 원인 불명 ``FileNotFoundError`` 로 잡 FAILED 가 됐다.
        """
        for fname in self.fixture_files:
            path = self.samples_dir / fname
            if not path.exists():
                raise FileNotFoundError(
                    f"fixture not found: {path} — samples_dir 설정(RAG_SAMPLES_DIR)과 "
                    f"samples/ 픽스처 존재를 확인하세요(저장소 기본: samples/{fname})"
                )
            data = json.loads(path.read_text(encoding="utf-8"))
            yield from data.get("single_page_responses", [])

    def _synthesize_acl(self, space_key: str) -> tuple[list[str], list[str]]:
        """PoC ACL 합성 — space_key 기반 그룹. 실제 ACL 연동 시 교체한다."""
        return [f"space:{space_key}"], []

    def _map_page(self, raw: dict) -> PageObject:
        """Atlassian 페이지 응답(dict) → 표준 PageObject."""
        space_key = raw["space"]["key"]
        space_id = str(raw["space"].get("id") or "")
        space_name = str(raw["space"].get("name") or "")
        allowed_groups, allowed_users = self._synthesize_acl(space_key)
        last_modified = parse_atlassian_datetime(raw["version"]["when"])
        labels = [
            label["name"] for label in raw.get("metadata", {}).get("labels", {}).get("results", [])
        ]
        ancestors = [ancestor["title"] for ancestor in raw.get("ancestors", [])]
        return PageObject(
            page_id=raw["id"],
            space_key=space_key,
            space_id=space_id,
            space_name=space_name,
            title=raw["title"],
            body_html=raw["body"]["storage"]["value"],
            version_number=raw["version"]["number"],
            last_modified=last_modified,
            allowed_groups=allowed_groups,
            allowed_users=allowed_users,
            webui_link=raw["_links"]["webui"],
            labels=labels,
            ancestors=ancestors,
            attachments=self._map_attachments(raw, last_modified),
        )

    def _map_attachments(self, raw: dict, last_modified: datetime) -> list[Attachment]:
        """페이지 attachments[] → Attachment 목록.

        샘플 JSON은 첨부 메타(filename/content_type)만 가지므로 누락 필드를 합성한다.
        download_url은 사용자 노출용 URI(file:// scheme)이며, 청커가 직접 열 실제 경로는
        local_path에 채운다 (ADR-2026-001). extracted_text는 빈 문자열로 두고, 분석기가
        빈 텍스트 + local_path 조합을 파일 기반 추출(chunk_attachment)로 위임한다(P1-3).
        """
        page_id = raw["id"]
        attachments: list[Attachment] = []
        for index, item in enumerate(raw.get("attachments", [])):
            filename = item["filename"]
            mime_type = item["content_type"]
            local_path = (self.samples_dir / "attachments" / filename).resolve()
            attachments.append(
                Attachment(
                    attachment_id=f"{page_id}-att-{index}",
                    filename=filename,
                    mime_type=mime_type,
                    extracted_text="",
                    extracted_format=infer_extracted_format(mime_type),
                    download_url=local_path.as_uri(),
                    local_path=str(local_path),
                    parent_page_id=page_id,
                    last_modified=last_modified,
                )
            )
        return attachments
