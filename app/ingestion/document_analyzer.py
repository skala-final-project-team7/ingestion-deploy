"""문서 분석기 [Agent] — 스페이스 단위 doc_type 판별 (FR-003).

--------------------------------------------------
작성자 : 최태성
담당 영역 : ingestion
작성목적 : 본문 doc_type 을 6유형(incident/operation/faq/meeting/adr/troubleshoot)으로
          판별하는 분석기. 스페이스 단위 1회 LLM(GPT-4o-mini, Function Calling) 호출 결과를
          MySQL `space_doc_type_cache` 에 캐싱하고, 이후 같은 스페이스 페이지는 캐시를
          재사용한다(`app/CLAUDE.md` §5, db-schema §3.1). LLM 호출은 어댑터 경계
          (`DocTypeClassifier`)에 격리해 테스트는 Fake 로 대체한다(비결정론 격리).
작성일 : 2026-05-26 (featureI-4b)
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-26, 최초 작성, featureI-4b — DocTypeClassifier ABC + Fake/OpenAI 구현 +
    DocumentAnalyzer.resolve_doc_type(캐시 우선 → 분류 → 캐싱 → 폴백).
--------------------------------------------------
[분류 컴포넌트 = Agent] 비결정론(LLM). 신뢰도 < 0.6 또는 호출 실패 시 ``DocType.OPERATION``
폴백(DocType 에 'general' 값이 없어 chunker 폴백·db-schema confidence 주석과 정합).
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - openai>=1.30 (OpenAIDocTypeClassifier 가 사용 — 지연 import)
  - 외부 의존성 0 (ABC + FakeDocTypeClassifier + DocumentAnalyzer 는 openai 미설치에서도 동작)
--------------------------------------------------
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.schemas.enums import DocType
from app.schemas.page_object import PageObject
from app.storage.space_doc_type_cache import SpaceDocTypeCache, SpaceDocTypeEntry

#: 신뢰도 하한 — 미만이면 OPERATION 폴백 (db-schema §3.1 confidence < 0.6).
CONFIDENCE_FLOOR = 0.6
#: 분류 실패·저신뢰 시 폴백 유형 (DocType 에 'general' 부재 → operation).
FALLBACK_DOC_TYPE = DocType.OPERATION
#: LLM 에 전달할 본문 샘플 최대 길이(토큰·비용 상한).
_SAMPLE_BODY_LIMIT = 2000


@dataclass(slots=True)
class DocTypeClassification:
    """분류 [Agent] 결과 — 어댑터 경계의 표준 출력."""

    dominant: DocType
    confidence: float
    secondary: list[DocType] = field(default_factory=list)


class DocTypeClassifier(ABC):
    """doc_type 분류 어댑터 추상 인터페이스 — DocumentAnalyzer 가 호출한다."""

    @abstractmethod
    def classify(self, *, space_key: str, sample_text: str) -> DocTypeClassification:
        """샘플 텍스트로 스페이스의 지배적 doc_type 을 분류한다.

        Raises:
            Exception: LLM 호출 실패(타임아웃·네트워크·스키마 위반 등). 호출자
                (DocumentAnalyzer)가 폴백을 적용한다.
        """


@dataclass(slots=True)
class FakeDocTypeClassifier(DocTypeClassifier):
    """결정론적 Fake 분류기 — 테스트·PoC 용(외부 의존성 0).

    ``result`` 를 그대로 반환하고, ``error`` 가 설정되면 호출 시 raise 한다(폴백 경로 테스트).
    호출 횟수(``calls``)를 기록해 캐시 히트로 LLM 재호출이 없음을 검증할 수 있다.
    """

    result: DocTypeClassification | None = None
    error: Exception | None = None
    calls: int = 0

    def classify(self, *, space_key: str, sample_text: str) -> DocTypeClassification:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise RuntimeError("FakeDocTypeClassifier.result 미설정")
        return self.result


class OpenAIDocTypeClassifier(DocTypeClassifier):
    """GPT-4o-mini + Function Calling 기반 분류기 — 운영 경로.

    Function Calling 으로 출력 스키마(6유형 enum + confidence + secondary)를 강제한다
    (`app/CLAUDE.md` §5). 타임아웃을 지정하고, 실패는 예외로 전파해 DocumentAnalyzer 가
    폴백한다(production path 에 실험성 모델 직결 금지).

    Args:
        api_key: OpenAI API key(SecretStr 평문). 로그에 남기지 않는다.
        model: 모델명(기본 gpt-4o-mini).
        timeout_seconds: 요청 타임아웃.
    """

    _DOC_TYPE_VALUES = [doc_type.value for doc_type in DocType]

    def __init__(
        self, *, api_key: str, model: str = "gpt-4o-mini", timeout_seconds: float = 20.0
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds

    def classify(self, *, space_key: str, sample_text: str) -> DocTypeClassification:
        from openai import OpenAI

        # 외부 SDK 경계 — payload(dict)·응답을 느슨하게 다룬다(SDK TypedDict 오버로드에
        # 우리 dict 가 정확히 매칭되지 않으므로 client 를 Any 로 두어 경계에서 격리).
        client: Any = OpenAI(api_key=self._api_key, timeout=self._timeout_seconds)
        tool = {
            "type": "function",
            "function": {
                "name": "classify_doc_type",
                "description": "Confluence 스페이스의 지배적 본문 문서 유형을 분류한다.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dominant": {"type": "string", "enum": self._DOC_TYPE_VALUES},
                        "secondary": {
                            "type": "array",
                            "items": {"type": "string", "enum": self._DOC_TYPE_VALUES},
                        },
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    },
                    "required": ["dominant", "confidence"],
                },
            },
        }
        response = client.chat.completions.create(
            model=self._model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "너는 사내 위키 문서 분류기다. 주어진 스페이스 샘플을 보고 지배적 문서 "
                        "유형 하나를 classify_doc_type 함수로 답하라."
                    ),
                },
                {"role": "user", "content": f"space_key={space_key}\n---\n{sample_text}"},
            ],
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": "classify_doc_type"}},
        )
        arguments = response.choices[0].message.tool_calls[0].function.arguments
        return _classification_from_arguments(arguments)


def _classification_from_arguments(arguments: str) -> DocTypeClassification:
    """Function Calling arguments(JSON) → DocTypeClassification(스키마 검증 포함)."""
    payload = json.loads(arguments)
    return DocTypeClassification(
        dominant=DocType(payload["dominant"]),
        confidence=float(payload["confidence"]),
        secondary=[DocType(value) for value in payload.get("secondary", [])],
    )


class DocumentAnalyzer:
    """스페이스 단위 doc_type 판별기 — 캐시 우선, 미스 시 분류·캐싱, 실패 시 폴백.

    Args:
        classifier: doc_type 분류 어댑터(Fake / OpenAI).
        cache: ``space_doc_type_cache`` 어댑터.
        confidence_floor: 폴백 적용 신뢰도 하한(기본 0.6).
    """

    def __init__(
        self,
        *,
        classifier: DocTypeClassifier,
        cache: SpaceDocTypeCache,
        confidence_floor: float = CONFIDENCE_FLOOR,
    ) -> None:
        self._classifier = classifier
        self._cache = cache
        self._confidence_floor = confidence_floor

    def resolve_doc_type(self, page: PageObject) -> DocType:
        """페이지 스페이스의 doc_type 을 결정한다(스페이스 1회 판별 → 캐시 재사용).

        캐시 히트면 LLM 재호출 없이 반환. 미스면 페이지 샘플로 1회 분류 후 캐싱한다.
        분류 실패는 폴백(OPERATION)을 반환하되 **캐싱하지 않아** 다음 페이지에서 재시도한다.
        저신뢰(< floor)는 OPERATION 으로 폴백하되 캐싱한다(반복 호출 방지).
        """
        cached = self._cache.get(page.space_key)
        if cached is not None:
            return cached.dominant_doc_type

        try:
            classification = self._classifier.classify(
                space_key=page.space_key, sample_text=_sample_text(page)
            )
        except Exception:
            # 일시적 실패는 캐싱하지 않고 폴백 — 다음 페이지에서 재시도(app/CLAUDE.md §5 Fallback).
            return FALLBACK_DOC_TYPE

        dominant = (
            classification.dominant
            if classification.confidence >= self._confidence_floor
            else FALLBACK_DOC_TYPE
        )
        self._cache.set(
            SpaceDocTypeEntry(
                space_key=page.space_key,
                dominant_doc_type=dominant,
                secondary_doc_types=classification.secondary,
                confidence=classification.confidence,
                analyzed_at=datetime.now(UTC),
                sample_count=1,
            )
        )
        return dominant


def _sample_text(page: PageObject) -> str:
    """LLM 분류 입력 — 제목·라벨·본문 일부를 결합(상한 길이로 절단)."""
    labels = " ".join(page.labels)
    return f"{page.title}\n{labels}\n{page.body_html[:_SAMPLE_BODY_LIMIT]}"
