"""Multi-Pool Vector Store — Qdrant Point payload 구성 [Storage].

--------------------------------------------------
작성자 : 최태성
작성목적 : LINA RAG 파이프라인의 Multi-Pool Vector Store(Qdrant) 적재용 Point payload를
          구성한다. 청크를 db-schema.md §1.2 payload 스키마로 변환하며, 세 Pool
          (title/content/label)은 동일한 payload 스키마를 공유한다.
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature5-A — Pool 이름 상수 + build_point_payload (순수 로직)
  - 2026-05-17, 코드 리뷰 후속(P2) — doc_type이 enum이 된 후에도 동일 JSON을 직렬화하도록
    .value 변환 명시 (ChunkMetadata.doc_type을 DocType|AttachmentType으로 강제한 결과 반영)
  - 2026-05-18, feature5-B-2 발견 보정 — Qdrant Point ID는 UUID/uint64만 받으므로
    SHA1 hex(40자) chunk_id를 Point ID로 직접 사용 불가. 어댑터(app/storage/qdrant_client.py)
    가 uuid5(NAMESPACE_OID, chunk_id)로 매핑하고, 원본 chunk_id는 payload에 보존하도록
    payload에 chunk_id 필드 1개 추가 (additive). db-schema §1.2·§1.3 동시 갱신.
  - 2026-05-18, 5-A 후속 — Chunk 재구성 정합. 청커가 산출한 token_count를 payload에
    동봉해 검색 단계(_chunk_from_search_hit)가 ChunkMetadata.token_count를 그대로
    복원할 수 있도록 한다 (additive). db-schema §1.2 동시 갱신.
  - 2026-06-10, A8 잔여 — payload 에 space_id/space_name 적재(rag search_node 복원 대상).
--------------------------------------------------
[호환성]
  - Python 3.11.x, Pydantic 2.7+
  - NOTE: 실제 Qdrant Collection 생성·Named Vector upsert·검색은 feature5-B(클라이언트
          연동) 책임이다. 본 모듈은 Chunk → Point payload 변환(순수 로직)만 제공한다.
--------------------------------------------------
"""

from typing import Any

from app.schemas.chunk import Chunk

# db-schema.md §1 — Multi-Pool Collection 이름 (app/config.py 기본값과 정합)
TITLE_POOL = "title_pool"
CONTENT_POOL = "content_pool"
LABEL_POOL = "label_pool"
POOL_NAMES = (TITLE_POOL, CONTENT_POOL, LABEL_POOL)

# db-schema.md §1.2 — text_preview는 청크 본문 첫 200자 (UI 출처 카드용 미리보기)
TEXT_PREVIEW_LIMIT = 200


def build_point_payload(chunk: Chunk, version_number: int) -> dict[str, Any]:
    """Chunk를 Qdrant Point payload로 변환한다 (db-schema.md §1.2).

    세 Pool(title/content/label)이 동일한 payload 스키마를 공유한다. Qdrant Point ID는
    UUID 또는 unsigned int만 허용하므로, 어댑터(``app/storage/qdrant_client.py``)에서
    ``uuid5(NAMESPACE_OID, chunk_id)`` 로 SHA1 hex chunk_id를 결정론 UUID로 매핑한다.
    원본 chunk_id는 검색 결과에서 복원할 수 있도록 payload에 함께 저장한다.
    ``version_number`` 는 페이지 단위 값이라 ChunkMetadata에 없으므로 부모
    PageObject에서 받아 별도 인자로 주입한다 — 재색인 시 멱등성 검사용.

    Args:
        chunk: 적재할 청크 (본문 텍스트 + 메타데이터 19종).
        version_number: 청크 부모 페이지의 버전. 재색인 멱등성 검사 키.

    Returns:
        db-schema.md §1.2 스키마의 Qdrant Point payload dict. datetime·enum 값은
        JSON 직렬화 가능한 문자열로 변환한다.
    """
    metadata = chunk.metadata
    return {
        "chunk_id": metadata.chunk_id,
        "page_id": metadata.page_id,
        "page_title": metadata.page_title,
        "section_header": metadata.section_header,
        "section_path": metadata.section_path,
        "chunk_index": metadata.chunk_index,
        "labels": list(metadata.labels),
        "doc_type": metadata.doc_type.value,
        "space_key": metadata.space_key,
        # 2026-06-10(A8 잔여) — 출처 카드 spaceId/spaceName 원천(rag search_node 가 복원).
        "space_id": metadata.space_id,
        "space_name": metadata.space_name,
        "allowed_groups": list(metadata.allowed_groups),
        "allowed_users": list(metadata.allowed_users),
        "webui_link": metadata.webui_link,
        "last_modified": metadata.last_modified.isoformat(),
        "version_number": version_number,
        # soft-delete 플래그 (ADR 0003 항목 4). 신규/재색인 upsert 는 항상 False. 삭제 확정 시
        # store.soft_delete_by_* 가 True 로 set_payload 하고, rag 검색은 is_deleted=true 를
        # must_not 으로 제외한다. legacy 인덱스(필드 부재)는 미삭제로 간주(must_not 자연 통과).
        "is_deleted": False,
        "source_type": metadata.source_type.value,
        "attachment_id": metadata.attachment_id,
        "attachment_filename": metadata.attachment_filename,
        "attachment_mime": metadata.attachment_mime,
        "extracted_format": (
            metadata.extracted_format.value if metadata.extracted_format else None
        ),
        "token_count": metadata.token_count,
        # text: 청크 풀 텍스트 (db-schema §1.2, feature17c-7). 재순위화(Cross-Encoder)·
        # 답변 생성기가 200자 프리뷰가 아닌 풀 텍스트로 동작하도록 payload 에 동봉한다
        # (이전엔 chunk_lookup(Mongo) 별도 조회였으나 미적재 시 200자만 쓰여 recall 저하).
        # text_preview 는 UI 출처 카드용 미리보기로 유지한다.
        "text": chunk.text,
        "text_preview": chunk.text[:TEXT_PREVIEW_LIMIT],
    }
