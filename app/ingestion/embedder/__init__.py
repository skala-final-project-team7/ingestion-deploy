"""app.ingestion.embedder — Dense / Sparse 임베더 어댑터 패키지 [Pipeline].

분리 의도 (app/CLAUDE.md §8 — 외부 호출은 어댑터/클라이언트 계층으로 분리):

- ``base.py`` — ABC 인터페이스(``DenseEmbedder`` / ``SparseEmbedder``) + Fake 구현.
  외부 의존성 0. 단위 테스트는 이 모듈만으로 동작한다.
- ``dense.py`` — ``E5DenseEmbedder`` (sentence-transformers 래퍼). embedding extra 필요.
- ``sparse.py`` — ``BM25SparseEmbedder`` (fastembed 래퍼). embedding extra 필요.

실 어댑터(``E5DenseEmbedder`` / ``BM25SparseEmbedder``)는 무거운 의존성 때문에 본
``__init__`` 에서 re-export하지 않는다. 호출자는 명시적으로
``from app.ingestion.embedder.dense import E5DenseEmbedder`` 같이 import한다.

본 패키지는 입력 텍스트의 모델별 전처리(e5 ``passage: `` / ``query: `` 프리픽스 등)
와 출력 벡터의 형식(L2 정규화 / ``SparseVector`` 값 객체)을 인터페이스 계약으로 강제한다.
무엇을 임베딩할지(Pool 별 입력 텍스트 구성)는 feature5-A 순수 로직
(``app/ingestion/embedding.py::pool_embedding_texts``)이 담당하며, 본 어댑터는
어떻게 임베딩할지(모델 호출 + 정규화)만 담당한다.
"""

from app.ingestion.embedder.base import (
    DenseEmbedder,
    FakeDenseEmbedder,
    FakeSparseEmbedder,
    SparseEmbedder,
    SparseVector,
)

__all__ = [
    "DenseEmbedder",
    "FakeDenseEmbedder",
    "FakeSparseEmbedder",
    "SparseEmbedder",
    "SparseVector",
]
