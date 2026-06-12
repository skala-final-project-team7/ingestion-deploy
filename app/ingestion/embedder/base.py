"""Dual Embedding — Embedder 어댑터 추상 인터페이스 + Fake 구현 [Pipeline].

--------------------------------------------------
작성자 : 최태성
담당 영역 : ingestion
작성목적 : LINA RAG 파이프라인 Dual Embedding 단계의 외부 모델 어댑터 경계를 정의한다.
          Dense(multilingual-e5-large) / Sparse(BM25)가 무엇이든 동일한 인터페이스로
          호출되도록 강제하고, 외부 모델 없이도 단위 테스트가 통과하도록 결정론적인
          Fake 구현을 함께 제공한다 (rag-pipeline-design.md §5, db-schema.md §1·§1.1,
          app/CLAUDE.md §8 — 외부 호출은 어댑터/클라이언트 계층으로 분리).
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature5-B-1 — DenseEmbedder/SparseEmbedder ABC +
    SparseVector 값 객체 + FakeDenseEmbedder/FakeSparseEmbedder
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - 외부 의존성 0 (sentence-transformers·fastembed는 실 어댑터 모듈에서만 요구)
--------------------------------------------------
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True, slots=True)
class SparseVector:
    """Sparse 임베딩 결과 값 객체 — Qdrant Named Vector(sparse-bm25) upsert 입력과 정합.

    indices와 values는 같은 길이이며, indices는 토큰 인덱스(중복 없음, 오름차순 권장),
    values는 해당 토큰의 가중치(BM25 idf 모드 가중치 등)다. Collection의 idf modifier는
    Qdrant 측에서 처리되므로(`docs/db-schema.md` §1.1) 어댑터는 모델이 산출한 값을
    그대로 전달한다.
    """

    indices: tuple[int, ...]
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.indices) != len(self.values):
            raise ValueError(
                f"SparseVector indices/values 길이 불일치: "
                f"{len(self.indices)} vs {len(self.values)}"
            )


class DenseEmbedder(ABC):
    """Dense 임베딩 어댑터 추상 인터페이스.

    e5-large 등 sentence embedding 모델 래퍼는 이 클래스를 상속하여 구현한다.
    Ingestion(`encode_passages`)과 Query(`encode_queries`)는 모델별로 다른 전처리
    (e5의 `passage: ` / `query: ` 프리픽스 등)가 필요하므로 메서드를 분리한다.
    반환 벡터는 L2 정규화된 상태여야 한다 (Cosine 유사도 검색 정합 — `docs/db-schema.md` §1.1).
    """

    @property
    @abstractmethod
    def dimension(self) -> int:
        """모델 임베딩 차원. e5-large = 1024."""

    @abstractmethod
    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        """Ingestion 단계 텍스트(저장 대상)를 dense 벡터로 변환한다.

        Args:
            texts: 임베딩할 원문 텍스트 목록 (모델 고유 프리픽스는 어댑터가 부착한다).

        Returns:
            텍스트마다 차원 ``self.dimension`` 의 L2 정규화된 dense 벡터 목록.
            입력이 비어있으면 빈 목록을 반환한다.
        """

    @abstractmethod
    def encode_queries(self, texts: list[str]) -> list[list[float]]:
        """Query 단계 텍스트(검색 입력)를 dense 벡터로 변환한다.

        Args:
            texts: 임베딩할 질의 텍스트 목록.

        Returns:
            텍스트마다 차원 ``self.dimension`` 의 L2 정규화된 dense 벡터 목록.
        """


class SparseEmbedder(ABC):
    """Sparse 임베딩 어댑터 추상 인터페이스 (BM25 등).

    Dense와 마찬가지로 ingestion/query 분리. BM25 자체는 동일한 토크나이저를 쓰지만,
    fastembed처럼 모드 분기를 명시적으로 노출하는 라이브러리와 정합하도록 두 메서드를
    분리한다. idf modifier 적용은 Qdrant Collection 설정 측 책임이다
    (`docs/db-schema.md` §1.1).
    """

    @abstractmethod
    def encode_passages(self, texts: list[str]) -> list[SparseVector]:
        """Ingestion 단계 텍스트를 sparse 벡터로 변환한다."""

    @abstractmethod
    def encode_queries(self, texts: list[str]) -> list[SparseVector]:
        """Query 단계 텍스트를 sparse 벡터로 변환한다."""


# --- Fake 구현 (테스트·PoC용 — 외부 모델 다운로드 회피) ---


class FakeDenseEmbedder(DenseEmbedder):
    """결정론적 해시 기반 Dense 임베더 — 테스트·PoC용 (외부 의존성 0).

    같은 입력 → 같은 벡터(재현성 보장). 실제 multilingual-e5-large 모델 다운로드(약
    2.24 GB) 없이 단위 테스트가 통과하도록 한다. Cosine 검색 정합을 위해 L2 정규화된
    벡터를 반환하며, passage/query는 다른 프리픽스로 다른 벡터를 만든다.
    """

    def __init__(self, dimension: int = 1024) -> None:
        if dimension < 4:
            raise ValueError("FakeDenseEmbedder dimension은 4 이상이어야 한다")
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._encode(text, prefix="passage") for text in texts]

    def encode_queries(self, texts: list[str]) -> list[list[float]]:
        return [self._encode(text, prefix="query") for text in texts]

    def _encode(self, text: str, *, prefix: str) -> list[float]:
        # 결정론적 해시 → dimension 길이의 float 벡터 → L2 정규화
        seed = sha256(f"{prefix}:{text}".encode()).digest()
        raw: list[float] = []
        cursor = 0
        while len(raw) < self._dimension:
            byte = seed[cursor % len(seed)]
            # [-1.0, 1.0) 범위로 매핑 — 0~255 → -1.0 ~ 1.0
            raw.append((byte / 127.5) - 1.0)
            cursor += 1
        norm = sum(value * value for value in raw) ** 0.5
        if norm == 0.0:
            # 영벡터 방지 — 첫 원소만 1.0으로 두고 normalized 처리 우회
            raw[0] = 1.0
            norm = 1.0
        return [value / norm for value in raw]


class FakeSparseEmbedder(SparseEmbedder):
    """결정론적 토큰 인덱스 기반 Sparse 임베더 — 테스트·PoC용 (외부 의존성 0).

    공백 토크나이즈 + 토큰 해시 → 인덱스, TF → 값. fastembed BM25와 같은 인터페이스를
    만족하되 외부 모델 없이 결정론적으로 동작한다. 실제 idf 가중치 계산은 fastembed가
    코퍼스 통계로 처리하므로 Fake에서는 단순 TF로 대체한다.
    """

    def __init__(self, *, vocab_size: int = 100_000) -> None:
        if vocab_size < 16:
            raise ValueError("FakeSparseEmbedder vocab_size는 16 이상이어야 한다")
        self._vocab_size = vocab_size

    def encode_passages(self, texts: list[str]) -> list[SparseVector]:
        return [self._encode(text) for text in texts]

    def encode_queries(self, texts: list[str]) -> list[SparseVector]:
        return [self._encode(text) for text in texts]

    def _encode(self, text: str) -> SparseVector:
        tokens = text.lower().split()
        if not tokens:
            return SparseVector(indices=(), values=())
        counts: dict[int, int] = {}
        for token in tokens:
            index = int.from_bytes(sha256(token.encode()).digest()[:4], "big") % self._vocab_size
            counts[index] = counts.get(index, 0) + 1
        # Qdrant Sparse Vector는 indices가 정렬되지 않아도 동작하지만, 결정론·디버깅 편의를
        # 위해 오름차순으로 정렬해 반환한다.
        sorted_items = sorted(counts.items())
        return SparseVector(
            indices=tuple(index for index, _ in sorted_items),
            values=tuple(float(count) for _, count in sorted_items),
        )
