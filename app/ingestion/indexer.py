"""Ingestion Indexer — 청크 → 임베딩 → Multi-Pool upsert 오케스트레이터 [Pipeline].

--------------------------------------------------
작성자 : 최태성
작성목적 : feature5-A(payload·멱등성 순수 로직) · 5-B-1(Embedder 어댑터) · 5-B-2(Qdrant
          Multi-Pool 클라이언트) · 5-B-3(EmbeddingCache) 부품을 한 흐름으로 잇는다.
          청크 컬렉션을 받아 (chunk_id, version_number) 기반 멱등성 필터를 거쳐, Pool별
          배치 임베딩 → Qdrant upsert → embedding_cache 기록까지 수행한다
          (`docs/rag-pipeline-design.md` §5, `docs/db-schema.md` §1·§2.4, `app/CLAUDE.md` §4).
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature5-B-3 — index_chunks 오케스트레이터 + IndexerResult
  - 2026-05-18, 풀 텍스트 lookup Phase 2 — index_chunks 에 chunk_lookup +
    attachment_download_urls 인자 추가. 모든 Pool upsert + cache write 성공 후 단일
    upsert_many 배치로 chunk_lookup 적재 (db-schema §2.5). 본문 청크 download_url=None,
    첨부 청크는 매핑에서 조회해 채움. 미주입 환경(default None)은 적재 없이 legacy 동작.
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - 외부 의존성 0 (주입된 DenseEmbedder / SparseEmbedder / QdrantPoolStore /
    EmbeddingCache의 구체 구현이 외부 의존성을 갖는다)
--------------------------------------------------
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha1

from app.ingestion.embedder.base import DenseEmbedder, SparseEmbedder, SparseVector
from app.ingestion.embedding import pool_embedding_texts, should_skip_embedding
from app.ingestion.vector_store import CONTENT_POOL, POOL_NAMES
from app.schemas.chunk import Chunk
from app.schemas.enums import SourceType
from app.storage.chunk_lookup import ChunkLookupRecord, ChunkTextLookup
from app.storage.mongo_cache import EmbeddingCache
from app.storage.qdrant_client import QdrantPoolStore


@dataclass(slots=True)
class IndexerResult:
    """Indexer 실행 결과 — 운영 메트릭·테스트 어서션 양쪽에서 사용한다.

    Attributes:
        upserted_count: 실제로 임베딩·upsert가 실행된 청크 수.
        skipped_count: 캐시 히트로 스킵된 청크 수 (멱등성).
        skipped_chunk_ids: 스킵된 청크의 chunk_id 목록 — 디버깅용.
        upserted_chunk_ids: upsert된 청크의 chunk_id 목록 — 검증용.
    """

    upserted_count: int = 0
    skipped_count: int = 0
    skipped_chunk_ids: list[str] = field(default_factory=list)
    upserted_chunk_ids: list[str] = field(default_factory=list)


def _hash_dense_vector(vector: list[float]) -> str:
    """Dense 벡터의 결정론 해시 — embedding_cache.dense_hash 추적용 (db-schema §2.4)."""
    # 부동소수 텍스트 직렬화 후 SHA1. 임베딩이 동일하면 동일 해시.
    encoded = ",".join(f"{value:.6e}" for value in vector).encode("utf-8")
    return sha1(encoded).hexdigest()


def _hash_sparse_vector(sparse: SparseVector) -> str:
    """Sparse 벡터의 결정론 해시 — embedding_cache.sparse_hash 추적용."""
    payload = ",".join(
        f"{index}:{value:.6e}" for index, value in zip(sparse.indices, sparse.values, strict=True)
    ).encode("utf-8")
    return sha1(payload).hexdigest()


def index_chunks(
    chunks: Iterable[Chunk],
    *,
    version_by_page_id: dict[str, int],
    dense_embedder: DenseEmbedder,
    sparse_embedder: SparseEmbedder,
    store: QdrantPoolStore,
    cache: EmbeddingCache,
    chunk_lookup: ChunkTextLookup | None = None,
    attachment_download_urls: dict[str, str] | None = None,
) -> IndexerResult:
    """청크들을 멱등 인덱싱한다 — (chunk_id, version_number) 동치면 임베딩·upsert 스킵.

    4-phase 배치 처리:
        1. **Filter** — ``cache.get_cached_version(chunk_id) == version`` 청크는 스킵.
        2. **Embed** — 남은 청크에 대해 Pool별 입력 텍스트(``pool_embedding_texts``)를
           모아 dense / sparse 배치 임베딩. 네트워크·모델 라운드트립 최소화.
        3. **Upsert + cache write** — Pool별 배치 upsert 후 ``embedding_cache`` 갱신.
           cache write는 모든 Pool upsert가 성공한 뒤 마지막에 — 도중 실패하면 다음
           실행에서 다시 시도되도록 한다 (best-effort 멱등성).
        4. **chunk_lookup upsert (optional)** — ``chunk_lookup`` 주입 시 upserted 청크
           의 풀 텍스트·첨부 download_url 을 단일 ``upsert_many`` 배치로 적재한다
           (db-schema §2.5). cache hit으로 스킵된 청크는 적재 대상에서 제외 — 멱등성
           정합. cache write 이후에 수행 — chunk_lookup 적재 실패가 멱등성 캐시에
           영향을 주지 않도록 한다.

    Args:
        chunks: 인덱싱 대상 청크 컬렉션.
        version_by_page_id: ``page_id -> version_number`` 매핑. ``ChunkMetadata`` 에
            ``version_number`` 가 없어 부모 페이지 단위로 별도 주입한다 (5-A 정합).
            누락된 page_id의 청크는 ``KeyError`` 로 즉시 실패한다.
        dense_embedder: Pool 입력 텍스트를 dense 벡터로 변환. 실 어댑터(E5DenseEmbedder)
            또는 Fake 주입.
        sparse_embedder: Pool 입력 텍스트를 sparse 벡터로 변환.
        store: Qdrant Multi-Pool 저장소.
        cache: ``embedding_cache`` 어댑터.
        chunk_lookup: ``chunk_lookup`` 컬렉션 어댑터. ``None`` 이면 적재를 수행하지
            않는다 (legacy 호출자 호환). 운영 / PoC 부트스트랩은 본 인자를 명시 주입
            한다 (`app/api/deps.py`).
        attachment_download_urls: ``attachment_id -> download_url`` 매핑. 첨부 청크의
            ``chunk_lookup`` 적재 시점에 download_url 을 채우는 데 사용한다. 본문
            청크에는 사용되지 않고, 매핑에 없는 첨부 청크는 download_url=None 으로
            안전 fallback. ``None`` 이면 빈 dict 와 동등 — 모든 첨부 download_url 이
            None 으로 적재된다.

    Returns:
        ``IndexerResult`` — upserted / skipped 카운트 + chunk_id 목록.

    Raises:
        KeyError: ``version_by_page_id`` 에 청크의 ``page_id`` 가 없을 때.
    """
    chunks_list = list(chunks)
    result = IndexerResult()

    # --- Phase 1: 멱등성 필터 ---
    # 판정은 embedding.should_skip_embedding 으로 일원화한다(P4 — 종전에는 동일 비교를
    # 인라인 중복해 함수가 죽은 코드였다).
    to_index: list[tuple[Chunk, int]] = []
    for chunk in chunks_list:
        version = version_by_page_id[chunk.metadata.page_id]
        cached_version = cache.get_cached_version(chunk.metadata.chunk_id)
        if should_skip_embedding(version, cached_version):
            result.skipped_count += 1
            result.skipped_chunk_ids.append(chunk.metadata.chunk_id)
            continue
        to_index.append((chunk, version))

    if not to_index:
        return result

    # --- Phase 2: Pool별 배치 임베딩 ---
    # 각 Pool에 대해 인덱싱 청크들의 입력 텍스트를 모은다. pool_embedding_texts(5-A)가
    # 본문/첨부 분기와 Pool별 텍스트 구성(title/content/label)을 담당한다.
    pool_texts: dict[str, list[str]] = {pool: [] for pool in POOL_NAMES}
    for chunk, _ in to_index:
        texts_by_pool = pool_embedding_texts(chunk)
        for pool in POOL_NAMES:
            pool_texts[pool].append(texts_by_pool[pool])

    pool_dense: dict[str, list[list[float]]] = {}
    pool_sparse: dict[str, list[SparseVector]] = {}
    for pool in POOL_NAMES:
        pool_dense[pool] = dense_embedder.encode_passages(pool_texts[pool])
        pool_sparse[pool] = sparse_embedder.encode_passages(pool_texts[pool])

    # --- Phase 3a: Pool별 배치 upsert ---
    for pool in POOL_NAMES:
        items = [
            (chunk, version, pool_dense[pool][index], pool_sparse[pool][index])
            for index, (chunk, version) in enumerate(to_index)
        ]
        store.upsert_chunks_batch(pool, items)

    # --- Phase 3b: cache write (모든 Pool upsert 성공 이후) ---
    # CONTENT_POOL 의 벡터를 대표 해시로 기록 — db-schema §2.4의 dense_hash/sparse_hash는
    # 추적용 메타데이터로, skip 판정에는 사용되지 않는다(skip은 version_number 비교).
    computed_at = datetime.now(UTC)
    for index, (chunk, version) in enumerate(to_index):
        dense_hash = _hash_dense_vector(pool_dense[CONTENT_POOL][index])
        sparse_hash = _hash_sparse_vector(pool_sparse[CONTENT_POOL][index])
        cache.set_cached_version(
            chunk_id=chunk.metadata.chunk_id,
            version_number=version,
            dense_hash=dense_hash,
            sparse_hash=sparse_hash,
            computed_at=computed_at,
        )
        result.upserted_count += 1
        result.upserted_chunk_ids.append(chunk.metadata.chunk_id)

    # --- Phase 4: chunk_lookup 적재 (optional) ---
    # cache write 이후에 수행해 chunk_lookup 적재 실패가 멱등성 캐시 상태를 오염시키지
    # 않도록 한다 (도중 실패 시 다음 실행은 cache hit으로 스킵되므로 chunk_lookup 적재가
    # 누락될 수 있다 — 운영에서는 retry / 백필 잡으로 보강한다).
    if chunk_lookup is not None:
        url_map = attachment_download_urls or {}
        records = [
            ChunkLookupRecord(
                chunk_id=chunk.metadata.chunk_id,
                text=chunk.text,
                download_url=_resolve_download_url(chunk, url_map),
            )
            for chunk, _ in to_index
        ]
        chunk_lookup.upsert_many(records)

    return result


def _resolve_download_url(chunk: Chunk, url_map: dict[str, str]) -> str | None:
    """첨부 청크의 download_url 을 매핑에서 조회. 본문 청크는 항상 None.

    `Source.download_url` 정합 (`docs/db-schema.md` §2.5): 본문 청크는 download_url 을
    가지지 않고, 첨부 청크만 사용자 노출용 URL 을 갖는다. 매핑에 attachment_id 가
    없으면 None 으로 안전 fallback — 운영에서 매핑 누락이 검색 결과 표시를 막지 않도록.
    """
    if chunk.metadata.source_type is not SourceType.ATTACHMENT:
        return None
    attachment_id = chunk.metadata.attachment_id
    if attachment_id is None:
        return None
    return url_map.get(attachment_id)
