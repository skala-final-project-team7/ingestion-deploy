"""Dual Embedding — BM25SparseEmbedder (fastembed 어댑터) [Pipeline].

--------------------------------------------------
작성자 : 최태성
작성목적 : fastembed의 SparseTextEmbedding(``Qdrant/bm25``)을 래핑해 SparseEmbedder
          인터페이스(`app/ingestion/embedder/base.py`)를 구현한다. fastembed는 Qdrant
          호환 sparse vector(indices+values)를 직접 산출하며, idf modifier는 Qdrant
          Collection 설정(`sparse_vectors.modifier="idf"`)이 담당한다
          (`docs/db-schema.md` §1.1). 본 어댑터는 모델 출력 형식 변환만 책임진다.
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature5-B-1 — BM25SparseEmbedder
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - fastembed>=0.3 (pyproject [embedding] extra)
  - NOTE: 본 모듈은 fastembed 설치를 전제로 한다. embedding extra 미설치 환경에서는
          import 단계에서 ImportError 발생 — 테스트는 base.Fake 또는
          pytest.importorskip로 우회한다.
--------------------------------------------------
"""

from fastembed import SparseTextEmbedding

from app.ingestion.embedder.base import SparseEmbedder, SparseVector

_DEFAULT_MODEL = "Qdrant/bm25"


class BM25SparseEmbedder(SparseEmbedder):
    """fastembed ``SparseTextEmbedding(Qdrant/bm25)`` 어댑터.

    Sparse BM25 임베딩을 생성한다. fastembed는 Qdrant 호환 sparse vector(indices/values)를
    직접 반환하므로 본 어댑터는 모델 출력의 형식 변환(``SparseVector`` 값 객체)만
    수행한다. idf modifier 적용은 Qdrant Collection 설정 측 책임이다
    (`docs/db-schema.md` §1.1).

    Args:
        model_name: fastembed 모델 이름. 기본값 ``Qdrant/bm25``.
        cache_dir: fastembed 모델 캐시 디렉토리. ``None`` 이면 HOME 기본 위치를 사용한다.

    Raises:
        ImportError: fastembed 미설치 시 모듈 import 단계에서 발생.
    """

    def __init__(
        self,
        model_name: str = _DEFAULT_MODEL,
        *,
        cache_dir: str | None = None,
    ) -> None:
        self._model = SparseTextEmbedding(model_name=model_name, cache_dir=cache_dir)

    def encode_passages(self, texts: list[str]) -> list[SparseVector]:
        if not texts:
            return []
        return [self._to_sparse_vector(embedding) for embedding in self._model.embed(texts)]

    def encode_queries(self, texts: list[str]) -> list[SparseVector]:
        if not texts:
            return []
        # fastembed 일부 버전은 query 전용 메서드(query_embed)를 노출한다.
        # 메서드가 있으면 사용하고, 없으면 embed로 fallback한다.
        if hasattr(self._model, "query_embed"):
            return [
                self._to_sparse_vector(embedding) for embedding in self._model.query_embed(texts)
            ]
        return [self._to_sparse_vector(embedding) for embedding in self._model.embed(texts)]

    @staticmethod
    def _to_sparse_vector(embedding: object) -> SparseVector:
        # fastembed SparseEmbedding은 .indices / .values 를 numpy array로 노출한다.
        # 도메인 값 객체로 변환해 호출자가 numpy 의존을 갖지 않도록 한다.
        indices = getattr(embedding, "indices", None)
        values = getattr(embedding, "values", None)
        if indices is None or values is None:
            raise TypeError(
                f"fastembed embedding 형식 오류: {type(embedding).__name__} — "
                "indices/values 속성 누락"
            )
        return SparseVector(
            indices=tuple(int(i) for i in indices),
            values=tuple(float(v) for v in values),
        )
