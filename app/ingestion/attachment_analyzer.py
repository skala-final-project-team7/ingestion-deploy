"""첨부 파일 분석기 [Pipeline] — mime/확장자 분류 + 텍스트 유효성 검증 (feature6).

--------------------------------------------------
작성자 : 최태성
작성목적 : Document Source Adapter가 추출한 첨부 텍스트를 Ingestion 그래프가 신뢰
          가능한 입력으로 받아들이기 위한 분류·유효성 검증을 수행한다 (LLM 미호출,
          결정론적 → Pipeline 분류). 설계서 §3.3.B 정합 — ①유형 판별, ②텍스트
          유효성. 메타데이터 부착(③)은 청커 `build_attachment_metadata` 가, Adaptive
          Chunker 호출(④)은 Ingestion 그래프 노드가 책임 (책임 분리).
작성일 : 2026-05-18
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-18, 최초 작성, feature6 Phase 1 — AttachmentAnalysisResult 값 객체 +
    analyze_attachment 함수. mime/확장자 분류 + 200자 길이 검증 + 동일 문자 반복
    비율 > 80% 검증 (설계서 §3.3.B). ATTACH_ENCRYPTED 는 추출 단계(별도 어댑터)
    책임이므로 본 분석기에서 다루지 않는다.
  - 2026-06-10, 코드 리뷰 재점검(P1-3) — ``extracted_text`` 가 비어 있고 파일 원천
    (``local_path`` 또는 ``download_url``)이 있으면 텍스트 품질 게이트를 건너뛰고
    파일 기반 추출(chunk_attachment)로 위임한다. 종전에는 텍스트를 채우는 주체가
    없어(어댑터·분석기 docstring 이 서로를 가리킴) 모든 첨부가 LOW_QUALITY_ATTACH
    로 스킵 — 첨부 ingest 가 사실상 비활성이었다. ingestion 레포와 미러 유지.
--------------------------------------------------
[호환성]
  - Python 3.11.x
  - 외부 의존성 0 — 표준 라이브러리만 사용 (Attachment 스키마 의존)
--------------------------------------------------
"""

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.schemas.enums import AttachmentType, IngestionStatus
from app.schemas.page_object import Attachment

# 텍스트 유효성 임계값 (설계서 §3.3.B).
_MIN_TEXT_LENGTH = 200
_MAX_REPETITION_RATIO = 0.8

# mime 힌트 → AttachmentType. 부분 문자열 매칭 (예: "spreadsheetml.sheet" 포함이면 XLSX).
_MIME_HINTS: tuple[tuple[str, AttachmentType], ...] = (
    ("pdf", AttachmentType.PDF),
    ("wordprocessingml", AttachmentType.DOCX),
    ("msword", AttachmentType.DOCX),
    ("spreadsheetml", AttachmentType.XLSX),
    ("ms-excel", AttachmentType.XLSX),
    ("csv", AttachmentType.CSV),
)

# 확장자 → AttachmentType. mime 분류 실패 시 fallback.
_EXTENSION_TYPES: dict[str, AttachmentType] = {
    ".pdf": AttachmentType.PDF,
    ".docx": AttachmentType.DOCX,
    ".doc": AttachmentType.DOCX,
    ".xlsx": AttachmentType.XLSX,
    ".xls": AttachmentType.XLSX,
    ".csv": AttachmentType.CSV,
}


@dataclass(frozen=True, slots=True)
class AttachmentAnalysisResult:
    """첨부 파일 분석 결과 — Ingestion 그래프 노드가 본 결과로 청킹 진행 여부를 결정한다.

    Attributes:
        attachment_id: 분석 대상 첨부 식별자. jobs 적재 시 키로 사용.
        attachment_type: 판별된 첨부 유형. 분류 실패 시 ``None``.
        status: ``IngestionStatus`` — ``SUCCESS`` / ``UNSUPPORTED_ATTACH_TYPE`` /
            ``LOW_QUALITY_ATTACH``. ``ATTACH_ENCRYPTED`` 는 추출 단계 책임이므로 본
            분석기에서 발급하지 않는다.
        reason: 사람이 읽을 수 있는 짧은 사유 문구 — 디버깅·``ingestion_jobs.error``
            적재용. ``SUCCESS`` 일 때도 빈 문자열이 아닌 짧은 설명을 부여한다.
    """

    attachment_id: str
    attachment_type: AttachmentType | None
    status: IngestionStatus
    reason: str

    @property
    def analyzable(self) -> bool:
        """``SUCCESS`` 일 때만 True — 후속 청킹 단계로 진행해도 되는지 단일 신호."""
        return self.status is IngestionStatus.SUCCESS


def analyze_attachment(attachment: Attachment) -> AttachmentAnalysisResult:
    """첨부 파일을 ①분류 + ②유효성 검증한다 (설계서 §3.3.B).

    분기 우선순위는 ① → ②. 미지원 mime/확장자는 텍스트가 정상 길이여도
    ``UNSUPPORTED_ATTACH_TYPE`` 로 떨어진다 (분류 실패가 유효성 검증보다 우선).

    Args:
        attachment: 분석 대상 첨부. ``extracted_text`` 는 선택 입력이다 — 채워져
            있으면 텍스트 품질 게이트(②)를 적용하고, 비어 있으면 파일 원천
            (``local_path``/``download_url``)이 있는 한 파일 기반 추출(chunk_attachment)
            로 위임한다(P1-3). 본 함수는 추출을 수행하지 않는다.

    Returns:
        ``AttachmentAnalysisResult`` — Ingestion 그래프 노드가 status 분기로 청킹
        진행 여부 결정.
    """
    # ① 유형 판별
    attachment_type = _classify_attachment(attachment)
    if attachment_type is None:
        return AttachmentAnalysisResult(
            attachment_id=attachment.attachment_id,
            attachment_type=None,
            status=IngestionStatus.UNSUPPORTED_ATTACH_TYPE,
            reason=(
                f"미지원 mime/확장자: mime={attachment.mime_type!r}, "
                f"filename={attachment.filename!r}"
            ),
        )

    # ② 텍스트 유효성. extracted_text 가 비어 있으면 — 어댑터가 텍스트를 채우지 않는
    # 경로(fixture: local_path / atlassian: download_url→다운로더) — 길이 게이트로
    # 차단하지 않고 파일 기반 추출로 위임한다(P1-3). 추출 실패·암호화는 chunk_attachment
    # 단계가 첨부 단위로 격리한다.
    text = attachment.extracted_text
    if not text and (attachment.local_path or attachment.download_url):
        return AttachmentAnalysisResult(
            attachment_id=attachment.attachment_id,
            attachment_type=attachment_type,
            status=IngestionStatus.SUCCESS,
            reason="extracted_text 없음 — 파일 기반 추출(chunk_attachment)로 위임",
        )

    if len(text) < _MIN_TEXT_LENGTH:
        return AttachmentAnalysisResult(
            attachment_id=attachment.attachment_id,
            attachment_type=attachment_type,
            status=IngestionStatus.LOW_QUALITY_ATTACH,
            reason=f"text length {len(text)} < {_MIN_TEXT_LENGTH}",
        )

    # ② 텍스트 유효성 — 동일 문자 반복 비율
    ratio = _max_char_repetition_ratio(text)
    if ratio > _MAX_REPETITION_RATIO:
        return AttachmentAnalysisResult(
            attachment_id=attachment.attachment_id,
            attachment_type=attachment_type,
            status=IngestionStatus.LOW_QUALITY_ATTACH,
            reason=f"동일 문자 반복 비율 {ratio:.2f} > {_MAX_REPETITION_RATIO}",
        )

    return AttachmentAnalysisResult(
        attachment_id=attachment.attachment_id,
        attachment_type=attachment_type,
        status=IngestionStatus.SUCCESS,
        reason="ok",
    )


def _classify_attachment(attachment: Attachment) -> AttachmentType | None:
    """mime → 확장자 순으로 AttachmentType 을 판별한다. 둘 다 실패하면 None."""
    mime_lower = attachment.mime_type.lower()
    for hint, attachment_type in _MIME_HINTS:
        if hint in mime_lower:
            return attachment_type

    extension = Path(attachment.filename).suffix.lower()
    return _EXTENSION_TYPES.get(extension)


def _max_char_repetition_ratio(text: str) -> float:
    """공백·개행을 제외한 문자 중 가장 빈도 높은 문자의 비율 (0.0 ~ 1.0).

    설계서 §3.3.B "동일 문자 반복 비율 > 80%" 정합. 공백·개행을 제외하는 이유는
    들여쓰기·줄바꿈이 많은 정상 첨부에서 false positive 가 발생하지 않도록 의미
    있는 문자만 평가하기 위함이다.

    Args:
        text: 평가 대상 텍스트.

    Returns:
        의미 있는 문자가 1자라도 있으면 ``max_count / total_count``, 모두 공백이면
        0.0 (분기 거짓 — 길이 검증에서 이미 LOW_QUALITY_ATTACH 로 차단됨).
    """
    meaningful = [c for c in text if not c.isspace()]
    if not meaningful:
        return 0.0
    counts = Counter(meaningful)
    _char, most_common_count = counts.most_common(1)[0]
    return most_common_count / len(meaningful)
