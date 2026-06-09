"""Dual Embedding — E5DenseEmbedder (multilingual-e5-large 어댑터) [Pipeline].

--------------------------------------------------
작성자 : 최태성
작성목적 : sentence-transformers의 SentenceTransformer를 래핑해 DenseEmbedder
          인터페이스(`app/ingestion/embedder/base.py`)를 구현한다. e5 모델은 입력에
          모드별 프리픽스(`passage: ` / `query: `)를 요구하므로 어댑터가 해당
          전처리를 강제한다. Cosine 유사도 검색 정합을 위해 항상 L2 정규화된 벡터를
          반환한다 (`docs/db-schema.md` §1.1, app/CLAUDE.md §8).
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature5-B-1 — E5DenseEmbedder
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - sentence-transformers>=3.0 (pyproject [embedding] extra)
  - NOTE: 본 모듈은 sentence-transformers / torch 설치를 전제로 한다. embedding extra
          미설치 환경에서는 import 단계에서 ImportError 발생 — 테스트는 base.Fake 또는
          pytest.importorskip로 우회한다.
--------------------------------------------------
"""

from sentence_transformers import SentenceTransformer

from app.ingestion.embedder.base import DenseEmbedder

# e5 모델은 모드별 프리픽스를 요구한다 (모델 카드 명세).
# Ingestion 시 저장 대상 텍스트 / Query 시 검색 입력 텍스트에 각각 부착한다.
_E5_PASSAGE_PREFIX = "passage: "
_E5_QUERY_PREFIX = "query: "


class E5DenseEmbedder(DenseEmbedder):
    """intfloat/multilingual-e5-large 어댑터.

    sentence-transformers SentenceTransformer를 래핑해 ``DenseEmbedder`` 인터페이스를
    구현한다. Cosine 유사도 검색 정합을 위해 ``normalize_embeddings=True`` 로 인코딩
    하며, e5 모델의 모드별 프리픽스(``passage: `` / ``query: ``)는 어댑터가 부착한다.

    Args:
        model_name: 모델 이름. 기본값은 multilingual-e5-large(1024d).
        device: torch 장치(``"cpu"`` / ``"cuda"`` / ``"mps"`` 등). ``None`` 이면
            sentence-transformers가 자동 선택한다.
        batch_size: 인코딩 시 배치 크기. 메모리·속도 균형을 위한 튜닝 지점.

    Raises:
        ImportError: sentence-transformers 미설치 시 모듈 import 단계에서 발생.
        RuntimeError: 모델 차원 조회 실패 시.
    """

    def __init__(
        self,
        model_name: str = "intfloat/multilingual-e5-large",
        *,
        device: str | None = None,
        batch_size: int = 32,
    ) -> None:
        self._model = SentenceTransformer(model_name, device=device)
        self._batch_size = batch_size
        dim = self._model.get_sentence_embedding_dimension()
        if dim is None:
            raise RuntimeError(f"모델 {model_name} 차원 조회 실패")
        self._dimension = dim

    @property
    def dimension(self) -> int:
        return self._dimension

    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts, prefix=_E5_PASSAGE_PREFIX)

    def encode_queries(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts, prefix=_E5_QUERY_PREFIX)

    def _encode(self, texts: list[str], *, prefix: str) -> list[list[float]]:
        if not texts:
            return []
        prefixed = [f"{prefix}{text}" for text in texts]
        # normalize_embeddings=True → L2 정규화된 벡터 반환(Cosine 검색 정합).
        # convert_to_numpy=True 후 list로 변환해 DenseEmbedder 시그니처와 일치시킨다.
        embeddings = self._model.encode(
            prefixed,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [vector.tolist() for vector in embeddings]
