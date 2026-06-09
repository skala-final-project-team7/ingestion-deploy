"""Multi-Pool Vector Store — Qdrant 클라이언트 어댑터 [Storage].

--------------------------------------------------
작성자 : 최태성
작성목적 : LINA RAG 파이프라인 Multi-Pool Vector Store(Qdrant) 어댑터. db-schema §1 의
          세 Pool(title/content/label)을 동일한 Named Vector 구조와 동일한 Payload
          스키마로 멱등 부트스트랩하고, Chunk + (dense, sparse) 벡터 쌍을 Named Vector
          upsert 하며, ACL 필터를 강제하는 Pool별 검색·키 기반 삭제를 노출한다
          (`docs/db-schema.md` §1.1·§1.2·§1.3·§1.4, `docs/rag-pipeline-design.md` §5·§6,
          `app/CLAUDE.md` §3·§8).
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature5-B-2 — QdrantPoolStore + SearchHit. Qdrant Point ID
    제약(UUID/uint64) 흡수를 위해 uuid5(NAMESPACE_OID, chunk_id)로 결정론 매핑.
  - 2026-05-18, feature6 Phase 3 — scroll_page_ids / scroll_attachment_ids 메서드 추가.
    설계서 §3.7 Reconciliation Phase 1 흐름의 ``set_B_pages`` / ``set_B_attaches``
    추출에 사용된다. CONTENT_POOL 하나만 스캔(3 Pool 동일 청크 적재 정합).
  - 2026-05-26, ADR 0003 항목 4 — soft-delete 도입. payload ``is_deleted`` BOOL 인덱스
    추가, 검색 결합 필터에 ``is_deleted=true`` ``must_not`` 제외, soft_delete_by_page_id /
    soft_delete_by_attachment_id (set_payload) 추가. hard delete 는 그대로 보존.
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - qdrant-client>=1.9 (project main dependency — `:memory:` 모드 지원 포함)
  - 테스트: `QdrantClient(":memory:")` in-process 인메모리 모드로 외부 컨테이너 없이 통합
    검증. payload 인덱스는 local 모드에서 UserWarning과 함께 noop이지만 필터 자체는 동작
    (real Qdrant 서버에서는 성능 인덱스로 동작).
--------------------------------------------------
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    Modifier,
    PayloadSchemaType,
    PointStruct,
    SparseVectorParams,
    VectorParams,
)
from qdrant_client.models import SparseVector as QdrantSparseVector

from app.config import Settings
from app.ingestion.embedder.base import SparseVector
from app.ingestion.vector_store import (
    CONTENT_POOL,
    LABEL_POOL,
    POOL_NAMES,
    TITLE_POOL,
    build_point_payload,
)
from app.schemas.chunk import Chunk

# Qdrant Collection 안에서 사용하는 Named Vector 이름 — db-schema.md §1.1 정합.
_DENSE_VECTOR_NAME = "dense"
_SPARSE_VECTOR_NAME = "sparse-bm25"

# Point ID 매핑용 결정론 네임스페이스. NAMESPACE_OID는 RFC 4122 표준의 잘 알려진 OID
# 네임스페이스(`2.16.840.1.113730.3.4.34`)로, 동일 chunk_id → 동일 UUID를 보장한다.
_POINT_ID_NAMESPACE = uuid.NAMESPACE_OID

# db-schema.md §1.3 — Payload 인덱스 필드 목록.
_KEYWORD_INDEX_FIELDS: tuple[str, ...] = (
    "chunk_id",
    "allowed_groups",
    "allowed_users",
    "space_key",
    "labels",
    "doc_type",
    "page_id",
    "attachment_id",
    "source_type",
)
_DATETIME_INDEX_FIELDS: tuple[str, ...] = ("last_modified",)
# soft-delete 플래그 인덱스 (ADR 0003 항목 4 — is_deleted must_not 제외 필터 성능).
_BOOL_INDEX_FIELDS: tuple[str, ...] = ("is_deleted",)


@dataclass(frozen=True, slots=True)
class SearchHit:
    """Qdrant 검색 결과 도메인 값 객체 — qdrant-client `ScoredPoint` 의존 격리.

    호출자는 본 객체만 다루며 qdrant-client 모델 타입은 어댑터에 격리된다. ``chunk_id`` 는
    payload에서 복원된 원본 SHA1 hex (Point ID로 쓰인 uuid5가 아님 — db-schema §1.2 참조).
    """

    chunk_id: str
    score: float
    payload: dict[str, Any]


def _chunk_id_to_point_id(chunk_id: str) -> str:
    """SHA1 hex ``chunk_id`` 를 결정론 UUID 문자열로 매핑한다.

    Qdrant Point ID는 unsigned int 또는 UUID 32자 표준 포맷만 허용하므로, 우리 SHA1
    hex(40자)는 그대로 사용할 수 없다. 동일 ``chunk_id`` → 동일 UUID를 보장해
    재upsert가 Qdrant 레벨에서도 멱등하도록 한다 (`app/CLAUDE.md` §4).
    """
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, chunk_id))


def _pool_name_to_collection(settings: Settings, pool_name: str) -> str:
    """``POOL_NAMES`` 상수를 Settings의 컬렉션 이름으로 매핑한다."""
    if pool_name == TITLE_POOL:
        return settings.qdrant_title_pool
    if pool_name == CONTENT_POOL:
        return settings.qdrant_content_pool
    if pool_name == LABEL_POOL:
        return settings.qdrant_label_pool
    raise ValueError(f"알 수 없는 pool 이름: {pool_name!r}")


class QdrantPoolStore:
    """Qdrant Multi-Pool Vector Store 어댑터 [Storage].

    db-schema §1 의 세 Pool(title/content/label)을 동일한 Named Vector 구조와 동일한
    Payload 스키마로 관리한다. 호출자는 Chunk + 벡터 쌍을 넘기고, ACL 필터(`@enforce_acl`
    가 검증한 dict)와 함께 검색을 요청한다. Hybrid Search(dense + sparse 결합)는
    feature9-A `reciprocal_rank_fusion`이 담당 — 본 어댑터는 단일 Named Vector 검색
    호출 두 번을 노출하는 데서 멈춘다.

    Args:
        client: 사전 구성된 qdrant-client 인스턴스. 운영은 ``QdrantClient(host, port)`` 를
            ``from_settings`` 로 생성하고, 테스트·시연은 ``QdrantClient(":memory:")`` 를
            ``in_memory`` 로 생성한다.
        settings: 환경 설정. 컬렉션 이름을 읽는다.
        dense_dimension: dense vector 차원. e5-large = 1024. 임베더의 ``dimension``
            속성을 그대로 주입한다. Collection 생성 시 ``VectorParams.size`` 로 박힌다.
    """

    def __init__(
        self,
        client: QdrantClient,
        settings: Settings,
        *,
        dense_dimension: int = 1024,
    ) -> None:
        self._client = client
        self._settings = settings
        self._dense_dimension = dense_dimension

    @classmethod
    def from_settings(cls, settings: Settings, *, dense_dimension: int = 1024) -> "QdrantPoolStore":
        """환경 설정에서 host/port로 클라이언트를 만들어 인스턴스화한다."""
        client = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
        return cls(client=client, settings=settings, dense_dimension=dense_dimension)

    @classmethod
    def in_memory(cls, settings: Settings, *, dense_dimension: int = 1024) -> "QdrantPoolStore":
        """qdrant-client ``:memory:`` 인메모리 클라이언트로 인스턴스화 (테스트·PoC)."""
        return cls(
            client=QdrantClient(":memory:"),
            settings=settings,
            dense_dimension=dense_dimension,
        )

    # --- 부트스트랩 ---

    def bootstrap_collections(self) -> None:
        """3 Pool 컬렉션을 멱등 생성하고 payload 인덱스를 부착한다.

        이미 존재하는 컬렉션은 건너뛴다. Named Vector 구성·Payload 인덱스 목록은
        db-schema §1.1·§1.3 정합. ``:memory:`` 로컬 모드에서는 payload 인덱스가 noop
        (UserWarning) 이지만 필터 매칭 자체는 동작한다.
        """
        for pool_name in POOL_NAMES:
            collection_name = _pool_name_to_collection(self._settings, pool_name)
            if not self._client.collection_exists(collection_name):
                self._create_collection(collection_name)
            self._ensure_payload_indexes(collection_name)

    def _create_collection(self, collection_name: str) -> None:
        # db-schema §1.1 — dense 1024d Cosine + sparse-bm25 idf, shard_number=2,
        # replication_factor=1, on_disk_payload=true. local 모드에서 일부 옵션은 무시됨.
        self._client.create_collection(
            collection_name=collection_name,
            vectors_config={
                _DENSE_VECTOR_NAME: VectorParams(
                    size=self._dense_dimension,
                    distance=Distance.COSINE,
                ),
            },
            sparse_vectors_config={
                _SPARSE_VECTOR_NAME: SparseVectorParams(modifier=Modifier.IDF),
            },
            shard_number=2,
            replication_factor=1,
            on_disk_payload=True,
        )

    def _ensure_payload_indexes(self, collection_name: str) -> None:
        for field in _KEYWORD_INDEX_FIELDS:
            self._client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        for field in _DATETIME_INDEX_FIELDS:
            self._client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=PayloadSchemaType.DATETIME,
            )
        for field in _BOOL_INDEX_FIELDS:
            self._client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=PayloadSchemaType.BOOL,
            )

    # --- Upsert ---

    def upsert_chunk(
        self,
        pool_name: str,
        chunk: Chunk,
        version_number: int,
        *,
        dense_vector: list[float],
        sparse_vector: SparseVector,
    ) -> None:
        """단일 Chunk을 지정 Pool에 upsert한다.

        Point ID는 ``uuid5(NAMESPACE_OID, chunk.metadata.chunk_id)`` — 동일 ``chunk_id`` 의
        재호출은 Qdrant 레벨에서 overwrite. 실제 재임베딩 회피(이미 같은 ``version_number``
        면 임베딩·upsert 스킵)는 5-B-3 ``embedding_cache`` 책임이다 (`app/CLAUDE.md` §4).
        """
        self.upsert_chunks_batch(
            pool_name,
            [(chunk, version_number, dense_vector, sparse_vector)],
        )

    def upsert_chunks_batch(
        self,
        pool_name: str,
        items: Iterable[tuple[Chunk, int, list[float], SparseVector]],
    ) -> None:
        """다수 Chunk을 한 번에 upsert (네트워크 라운드트립 최소화)."""
        collection_name = _pool_name_to_collection(self._settings, pool_name)
        points = [
            self._build_point(chunk, version_number, dense_vector, sparse_vector)
            for chunk, version_number, dense_vector, sparse_vector in items
        ]
        if not points:
            return
        self._client.upsert(collection_name=collection_name, points=points)

    def _build_point(
        self,
        chunk: Chunk,
        version_number: int,
        dense_vector: list[float],
        sparse_vector: SparseVector,
    ) -> PointStruct:
        payload = build_point_payload(chunk, version_number)
        return PointStruct(
            id=_chunk_id_to_point_id(chunk.metadata.chunk_id),
            vector={
                _DENSE_VECTOR_NAME: dense_vector,
                _SPARSE_VECTOR_NAME: QdrantSparseVector(
                    indices=list(sparse_vector.indices),
                    values=list(sparse_vector.values),
                ),
            },
            payload=payload,
        )

    # --- Search ---

    def search(
        self,
        pool_name: str,
        *,
        acl_filter: dict[str, Any],
        dense_vector: list[float] | None = None,
        sparse_vector: SparseVector | None = None,
        top_k: int = 20,
        metadata_filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchHit]:
        """단일 Pool 검색 — Named Vector(dense 또는 sparse) 한 종류만 받는다.

        Hybrid Search(dense + sparse 결합)는 호출자가 본 메서드를 두 번 호출하고
        feature9-A ``reciprocal_rank_fusion`` 으로 결합한다 (rag-pipeline-design.md §6 4.5).
        ACL 필터는 ``build_acl_filter`` 출력 형식의 dict를 그대로 받아 Qdrant Filter로
        파싱·결합한다. ``@enforce_acl`` 데코레이션은 호출 측(9-B 노드)의 책임이며, 본
        메서드는 ``acl_filter`` 를 필수 키워드 인자로 강제해 미주입 호출을 시그니처 수준에서
        차단한다.

        Args:
            pool_name: ``TITLE_POOL`` / ``CONTENT_POOL`` / ``LABEL_POOL`` 중 하나.
            acl_filter: ``build_acl_filter`` 출력 dict (e.g. ``{"should": [...]}``).
            dense_vector: dense 검색 시 query 벡터 (L2 정규화 권장).
            sparse_vector: sparse 검색 시 query SparseVector.
            top_k: 상위 N개. 기본 20.
            metadata_filters: 추가 메타데이터 필터. ``{"doc_type": "incident",
                "space_key": ["CLOUD", "CCC"]}`` 처럼 값이 str이면 ``MatchValue``,
                list면 ``MatchAny`` 로 변환된다.

        Returns:
            점수 내림차순 ``SearchHit`` 목록.

        Raises:
            ValueError: dense·sparse 둘 다 입력하거나 둘 다 비어있을 때.
        """
        if dense_vector is None and sparse_vector is None:
            raise ValueError("dense_vector 또는 sparse_vector 중 하나는 필요하다")
        if dense_vector is not None and sparse_vector is not None:
            raise ValueError(
                "dense·sparse 동시 입력 미지원 — Hybrid는 두 번 호출 후 9-A RRF로 결합한다"
            )

        collection_name = _pool_name_to_collection(self._settings, pool_name)
        query_filter = self._build_combined_filter(acl_filter, metadata_filters)

        if dense_vector is not None:
            response = self._client.query_points(
                collection_name=collection_name,
                query=dense_vector,
                using=_DENSE_VECTOR_NAME,
                query_filter=query_filter,
                limit=top_k,
            )
        else:
            assert sparse_vector is not None  # noqa: S101 — 위 분기에서 이미 가드됨
            if not sparse_vector.indices:
                return []  # 빈 sparse 쿼리는 매칭이 무의미 — 명시적 short-circuit
            response = self._client.query_points(
                collection_name=collection_name,
                query=QdrantSparseVector(
                    indices=list(sparse_vector.indices),
                    values=list(sparse_vector.values),
                ),
                using=_SPARSE_VECTOR_NAME,
                query_filter=query_filter,
                limit=top_k,
            )

        return [
            SearchHit(
                chunk_id=str(point.payload["chunk_id"]) if point.payload else "",
                score=float(point.score),
                payload=dict(point.payload or {}),
            )
            for point in response.points
        ]

    def _build_combined_filter(
        self,
        acl_filter: dict[str, Any],
        metadata_filters: dict[str, str | list[str]] | None,
    ) -> Filter:
        # ACL은 `should` OR (allowed_groups any OR allowed_users any) — build_acl_filter
        # 출력 그대로. metadata는 `must` AND. 최종 Filter는 `must=[<metadata FieldConditions>,
        # <ACL Filter>]` 로 nested AND-of-OR.
        acl_filter_obj = Filter.model_validate(acl_filter)

        must_conditions: list[FieldCondition | Filter] = []
        if metadata_filters:
            for field, value in metadata_filters.items():
                if isinstance(value, list):
                    must_conditions.append(
                        Filter.model_validate({"should": [{"key": field, "match": {"any": value}}]})
                    )
                else:
                    must_conditions.append(FieldCondition(key=field, match=MatchValue(value=value)))
        must_conditions.append(acl_filter_obj)
        # soft-delete 제외 (ADR 0003 항목 4) — is_deleted=true Point 는 모든 검색에서 항상
        # 제외한다. payload 에 is_deleted 필드가 없는 legacy Point 는 true 와 매칭되지 않아
        # 자연히 통과한다(미삭제 간주 — 재색인 없이 후방 호환).
        deleted_exclusion = [FieldCondition(key="is_deleted", match=MatchValue(value=True))]
        # mypy: Filter.must 는 더 넓은 Condition union list를 받지만 list invariance 때문에
        # list[FieldCondition | Filter]가 호환되지 않는다. 런타임 contract는 만족(우리가 넣는
        # 두 타입 모두 그 union에 속함)하므로 좁힘만 우회.
        return Filter(must=must_conditions, must_not=deleted_exclusion)  # type: ignore[arg-type]

    # --- Delete ---

    # --- Reconciliation scroll (feature6 Phase 3) ---

    def scroll_page_ids(self, *, batch_size: int = 1000) -> set[str]:
        """본문 청크의 ``page_id`` unique set을 반환한다 — 삭제 동기화용 (설계서 §3.7).

        CONTENT_POOL 한 곳만 스캔한다 — 3 Pool에 동일 청크가 모두 적재되므로 한 Pool
        만 봐도 충분하다 (적재 멱등성, `app/CLAUDE.md` §4). source_type=page 필터로
        본문 청크만 추출, payload-only scroll로 벡터 미로드 (메모리 효율).

        Args:
            batch_size: 한 번에 가져올 Point 수. 기본 1000. 운영(수십만 청크) 튜닝
                지점.

        Returns:
            적재된 본문 청크의 page_id unique set. 빈 컬렉션이면 빈 set.
        """
        return self._scroll_payload_field("page_id", source_type="page", batch_size=batch_size)

    def scroll_attachment_ids(self, *, batch_size: int = 1000) -> set[str]:
        """첨부 청크의 ``attachment_id`` unique set을 반환한다 — 삭제 동기화용 (설계서 §3.7).

        ``scroll_page_ids`` 와 동일 패턴이나 source_type=attachment 필터를 적용하고
        ``attachment_id`` payload 필드를 추출한다.

        Returns:
            적재된 첨부 청크의 attachment_id unique set. 빈 컬렉션이면 빈 set.
        """
        return self._scroll_payload_field(
            "attachment_id", source_type="attachment", batch_size=batch_size
        )

    def _scroll_payload_field(self, field: str, *, source_type: str, batch_size: int) -> set[str]:
        """CONTENT_POOL 을 source_type 필터로 페이지네이션 scroll, payload 필드 unique set 추출."""
        collection_name = _pool_name_to_collection(self._settings, CONTENT_POOL)
        scroll_filter = Filter(
            must=[FieldCondition(key="source_type", match=MatchValue(value=source_type))]
        )
        unique: set[str] = set()
        offset: Any = None
        while True:
            records, next_offset = self._client.scroll(
                collection_name=collection_name,
                scroll_filter=scroll_filter,
                limit=batch_size,
                offset=offset,
                with_payload=[field],
                with_vectors=False,
            )
            for record in records:
                payload = record.payload or {}
                value = payload.get(field)
                if value is not None:
                    unique.add(str(value))
            if next_offset is None:
                break
            offset = next_offset
        return unique

    def delete_by_page_id(self, page_id: str) -> None:
        """``page_id`` 가 일치하는 모든 Point를 세 Pool에서 삭제한다 (문서 단위 삭제)."""
        self._delete_by_field("page_id", page_id)

    def delete_by_attachment_id(self, attachment_id: str) -> None:
        """``attachment_id`` 가 일치하는 모든 Point를 세 Pool에서 삭제한다."""
        self._delete_by_field("attachment_id", attachment_id)

    def delete_by_chunk_id(self, chunk_id: str) -> None:
        """``chunk_id`` 가 일치하는 단일 Point를 세 Pool에서 삭제한다."""
        self._delete_by_field("chunk_id", chunk_id)

    def _delete_by_field(self, field: str, value: str) -> None:
        selector = FilterSelector(
            filter=Filter(must=[FieldCondition(key=field, match=MatchValue(value=value))])
        )
        for pool_name in POOL_NAMES:
            collection_name = _pool_name_to_collection(self._settings, pool_name)
            if self._client.collection_exists(collection_name):
                self._client.delete(collection_name=collection_name, points_selector=selector)

    # --- Soft delete (ADR 0003 항목 4) ---

    def soft_delete_by_page_id(self, page_id: str) -> None:
        """``page_id`` 일치 Point 의 payload ``is_deleted`` 를 True 로 설정한다 (소프트 삭제).

        hard delete(``delete_by_page_id``)와 달리 Point 를 보존하고 검색에서만 제외한다
        (rag 검색이 ``is_deleted=true`` 를 ``must_not`` 으로 거른다). Trash/Webhook 삭제,
        복구·감사·지연 정리(GC)를 위한 경로다 (ADR 0003 항목 4, db-schema §1.2).
        """
        self._soft_delete_by_field("page_id", page_id)

    def soft_delete_by_attachment_id(self, attachment_id: str) -> None:
        """``attachment_id`` 일치 Point 의 ``is_deleted`` 를 True 로 설정한다 (소프트 삭제)."""
        self._soft_delete_by_field("attachment_id", attachment_id)

    def _soft_delete_by_field(self, field: str, value: str) -> None:
        # set_payload(payload, points=Filter) — 필터에 매칭되는 모든 Point 의 is_deleted 만
        # True 로 갱신하고 벡터·나머지 payload 는 보존한다(부분 업데이트).
        selector = Filter(must=[FieldCondition(key=field, match=MatchValue(value=value))])
        for pool_name in POOL_NAMES:
            collection_name = _pool_name_to_collection(self._settings, pool_name)
            if self._client.collection_exists(collection_name):
                self._client.set_payload(
                    collection_name=collection_name,
                    payload={"is_deleted": True},
                    points=selector,
                )
