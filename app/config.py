"""애플리케이션 환경 설정.

--------------------------------------------------
작성자 : 최태성
작성목적 : 데이터 공급원·Qdrant·MongoDB·MySQL·OpenAI·모델명 등 환경 의존 설정을
          환경 변수(RAG_ 프리픽스) 또는 .env 파일에서 주입받는다. 시크릿은
          코드에 포함하지 않는다 (루트 CLAUDE.md 절대 규칙).
작성일 : 2026-05-15
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-15, 최초 작성, feature1 — pydantic-settings 기반 Settings 정의
  - 2026-05-17, 코드 리뷰 후속(P1-1) — samples_dir이 어댑터에 흐르도록 정리,
    mysql_uri는 운영 전환 시 SecretStr 승급 후보 NOTE 명시
  - 2026-05-18, build_real_deps 후속 — use_real_adapters 토글 추가
    (RAG_USE_REAL_ADAPTERS). 기본값 False(PoC). True 시 lifespan이 build_real_deps
    분기로 E5 + BM25 + Qdrant from_settings + CrossEncoderRerankerImpl을 부트스트랩
  - 2026-06-09, api-spec v2.5.0 정합 — ingest_completion_routing_key 추가(수집 완료 이벤트
    RabbitMQ 라우팅 키). credential 미포함 payload 계약.
  - 2026-06-10, 코드 리뷰 재점검(A2) — atlassian_empty_restriction_policy 기본값을
    allow_authenticated → **mark_missing**(fail-closed)으로 변경. 상속 제한 문서
    과다 노출 방지(ancestor restriction 조회 구현 전까지 allow_authenticated 는 opt-in).
  - 2026-05-19, feature12 — cross_encoder_model 기본값에 ``-v2`` 추가.
    Hugging Face / sentence-transformers 의 실 모델명은 ``cross-encoder/ms-marco-
    MiniLM-L-12-v2`` 이며 ``-v2`` 가 없는 변형은 존재하지 않는다 (설계서
    §4.5.3 표기는 ``-v2`` 누락 — 설계서 차기 개정 시 반영 권장). 직전 세션
    까지는 ``.env`` 의 ``RAG_CROSS_ENCODER_MODEL`` 로 우회 중이었으며 본 fix 로
    코드 기본값만으로도 운영 모드(``RAG_USE_REAL_ADAPTERS=true``) 에서 모델
    로드 성공.
  - 2026-06-10, A2 후속 — atlassian_ancestor_restriction_lookup 토글 추가(기본 True).
    빈 restriction 페이지가 조상 제한을 상속해 정확한 ACL 로 색인된다(종전 mark_missing
    기본에선 색인 제외되던 페이지의 복구 — fail-closed 안전성은 유지).
  - 2026-06-11, backend-template 동기화(site_url 단일화 — db-schema §6.4/api-spec §2-5) —
    atlassian_site_url 주석을 확정 계약으로 갱신: BE 는 site_url 을
    admin_atlassian_credential.site_url 한 곳에만 저장하고(§2-5 JSON `siteUrl` 로 전달,
    public_site_url 별도 명칭 없음), 본 설정은 그 값의 env 주입 지점이다. 용도 ①
    admin-key 호출 대상 site ② webui_link absolute 정규화 base(출처 링크).
--------------------------------------------------
[호환성]
  - Python 3.11.x, Pydantic 2.7+, pydantic-settings 2.3+
--------------------------------------------------
"""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경 변수 기반 설정. 모든 항목은 기본값을 가지므로 무인자 인스턴스화가 가능하다."""

    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 데이터 공급원 (docs/atlassian-api.md) ---
    source_type: str = "json_fixture"  # json_fixture | atlassian
    samples_dir: str = "samples"
    # NOTE: access_token / cloudid 전달 경로는 미정(TBD) — docs/ai/current-plan.md 참조.
    # 아래는 PoC placeholder(env 주입). source_type="atlassian" 시 vendored Data Ingestion
    # Agent 에 전달된다. access_token 은 SecretStr 로 보관해 로그·직렬화에 노출하지 않는다.
    atlassian_api_base_url: str = "https://api.atlassian.com"
    atlassian_cloud_id: str = ""
    atlassian_access_token: SecretStr = SecretStr("")
    atlassian_request_delay_seconds: float = 0.3
    atlassian_max_retries: int = 3
    atlassian_timeout_seconds: int = 20
    # True면 admin-key credential 경로를 사용한다(api-spec v2.6.1 도입 → v2.6.2 ML 측
    # 단서로 보존): Confluence 호출을 admin API Token 의 Basic 인증
    # (base64(adminEmail:adminApiToken)) + Atl-Confluence-With-Admin-Key header 로
    # **site URL** 에서 수행하고, page-level read restriction
    # (/rest/api/content/{id}/restriction/byOperation/read)을 조회해
    # allowed_groups/allowed_users 를 산출한다. BE 확정 계약(v2.6.2 하이브리드)의 콘텐츠
    # 조회는 OAuth Bearer+게이트웨이+Admin Key 헤더이나 **3단계 검증 게이트** — ML 실측
    # (2026-06-02)에서는 동 조합으로 admin-key 우회가 재현되지 않아(위반 시 restricted
    # 페이지 무음 누락) 본 토글의 Basic+site URL 경로를 게이트 통과 시까지 보존한다.
    # False(기본)면 OAuth Bearer 사용자 토큰 경로(restriction 미조회 — ACL 없는 페이지는
    # fail-closed 로 색인 제외).
    # Admin Key 활성화/만료는 backend/infra 운영 영역이며, 본 설정은 클라이언트 구성만 제어한다.
    atlassian_use_admin_key: bool = False
    # --- admin-key credential (use_admin_key=True 시 필수 — v2.6.1 §1-4 ④⑤ 도입, v2.6.2 보존) ---
    # admin API Token 은 id.atlassian.com 에서 수동 발급한 정적 단일 credential 이다.
    # OAuth access token(로그인용)과 다른 물건이며 Basic 인증으로만 쓰인다. 저장/조회
    # 위치(DB vs env Secret)는 별도 결정 사항으로, 현 codebase 는 env Secret 주입을 쓴다.
    # --- site_url 단일화 (backend 2026-06-11 확정 — db-schema §6.4 V004 / api-spec §2-5) ---
    # site_url 은 BE DB `admin_atlassian_credential.site_url` **한 곳**에만 저장되고
    # (public_site_url 같은 별도 명칭 없음), §2-5 credential lookup 이 JSON `siteUrl` 로
    # ingestion 에 전달하는 값과 동일하다(accessible-resources 응답 url —
    # https://{site}.atlassian.net, secret 아님). 용도 2가지:
    #   ① admin-key 호출 대상 site (use_admin_key=True 경로의 base URL)
    #   ② 출처 링크 정규화 base — `_links.webui` 상대경로 → absolute
    #      (normalize_webui_link → Qdrant webui_link / RAG sources[].url)
    # 콘텐츠 조회 REST 게이트웨이(api.atlassian.com/ex/confluence/{cloudId})와 혼용 금지.
    # atlassian 소스에서 미주입 시 출처 링크가 상대경로로 남는다(bootstrap WARNING).
    atlassian_site_url: str = ""  # 예: https://{site}.atlassian.net (= §2-5 siteUrl)
    atlassian_admin_email: str = ""
    atlassian_admin_api_token: SecretStr = SecretStr("")
    # read restriction의 group 결과를 allowed_groups로 변환할 때 사용할 식별자 우선순위.
    # RAG JWT groups claim과 같은 문자열이어야 검색 ACL이 매칭된다(공유 계약).
    atlassian_group_acl_field_order: str = "id,groupId,name"
    # group 값 앞에 붙일 prefix. 기본값은 무변환이며, 예: "confluence-group:".
    atlassian_group_acl_prefix: str = ""
    # page-level read restriction이 비어 있을 때의 처리 정책.
    # mark_missing(기본): 빈 ACL → INVALID_ACL(색인 제외, fail-closed).
    # allow_authenticated(opt-in 전용): public_acl_group sentinel 부여 → 모든 인증 사용자 허용.
    # (space_fallback 은 2026-06-11 제거 — ACL 값에 space key 를 싣는 레거시 모델 폐기,
    #  회의 결정 "allowed_groups 내 스페이스 키 완전 제거". ADR 0002 superseded.)
    #   ⚠ Admin Key 실측에서 page restriction 이 비어도 상위(folder/page/space) 권한 상속으로
    #   조회가 막히는 사례가 확인됐다(adapters/atlassian.py docstring). ancestor restriction
    #   조회 구현 전까지 allow_authenticated 는 상속 제한 문서를 전체 인증 사용자에게 노출할
    #   수 있어 기본값으로 두지 않는다(코드 리뷰 A2 — 2026-06-10).
    atlassian_empty_restriction_policy: str = "mark_missing"
    # 2026-06-10(A2 후속) — 빈 page restriction 시 조상 체인 restriction 을 조회해 상속
    # ACL 을 산출한다(가까운 조상 우선, full crawl 은 payload parent_id 체인 재사용·delta 는
    # get_page_detail 워크 + 캐시). False 면 종전 동작(빈 restriction → 즉시 정책 폴백).
    atlassian_ancestor_restriction_lookup: bool = True
    # allow_authenticated 정책에서 부여할 "모든 인증 사용자" sentinel group 토큰.
    # 이 토큰이 실제 검색 허용이 되려면 RAG build_acl_filter가 동일 토큰을 모든
    # principal 그룹에 주입해야 한다(ingestion↔rag 공유 계약 — docs/db-schema.md §1.4).
    atlassian_public_acl_group: str = "*"

    # --- Qdrant Multi-Pool Vector Store ---
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_title_pool: str = "title_pool"
    qdrant_content_pool: str = "content_pool"
    qdrant_label_pool: str = "label_pool"

    # --- MongoDB (ingestion_jobs / embedding_cache) ---
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "lina_rag"

    # --- MySQL (space_doc_type_cache) ---
    # NOTE(P2): 운영 전환 시 비밀번호 포함 DSN이 들어오면 SecretStr로 승급해야 한다.
    # PoC는 localhost·비밀번호 없는 DSN만 사용하므로 평문 문자열을 유지한다.
    mysql_uri: str = "mysql+pymysql://localhost:3306/lina_rag"

    # --- RabbitMQ (운영 분산 파이프라인 — 배포 전 점검 fix, 2026-06-11) ---
    # use_real_adapters=True 시 HTTP API(full crawl/delta 발행·completion event)와 chunking
    # worker(소비)가 사용하는 AMQP URL. vhost 는 URL-encode 한다(기본 "/" = %2F).
    # 시크릿 포함 가능(amqp://user:pass@host)이므로 로그에 출력하지 않는다.
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/%2F"

    # --- 수집 완료 이벤트 (api-spec v2.5.0) ---
    # 수집 잡이 terminal(COMPLETED/FAILED) 상태에 도달하면 ML/Data Ingestion 이 RabbitMQ
    # completion event 를 발행한다. BFF consumer 가 이를 consume 해 auth-server 의 Admin Key
    # deactivate 내부 API 를 호출한다(ML 은 Admin Key 를 직접 말소하지 않음 — 책임 분리).
    # payload 에는 jobId/adminUserId/mode/status 만 담고 accessToken/refreshToken/cloudId 같은
    # credential set 은 절대 포함하지 않는다(루트 CLAUDE.md 보안 규칙).
    ingest_completion_routing_key: str = "ingestion.completed"

    # --- Delta Sync (FR-005) ---
    # mode=delta 가 vendored Data Sync Agent(run_delta_sync)로 직전 수집 스냅샷과 변경분을 비교할 때
    # 사용하는 이전 스냅샷 파일 경로. 운영에서는 raw store/스냅샷 repository 로 교체될 수 있으나
    # 현재 vendored 계약은 파일 경로를 요구한다.
    data_sync_previous_snapshot: str = "data/snapshots/latest_snapshot.json"
    # delta 삭제 후보(deleted_candidate_page_ids) 자동 soft-delete 게이트(확인 게이트 보존).
    # 기본 False = 후보를 surface 만(자동 삭제 안 함 — sync false-positive 로 유효 문서 삭제 방지).
    # True = /ml/ingest delta 잡이 후보를 SyncWorker.apply_delta_deletions(confirm=True)로 적용.
    data_sync_delta_delete_confirm: bool = False

    # --- Webhook 보호 (코드 리뷰 A18, 2026-06-10) ---
    # /ml/confluence/webhook 는 본문 id 를 즉시 soft-delete 하므로 직접 노출 시 임의 페이지
    # 대량 soft-delete 가 가능하다. 값이 설정되면 요청 헤더 X-Webhook-Secret 이 일치해야
    # 처리한다(불일치 401). 빈 값(기본)은 검증 생략 — BFF 전단 인증/NetworkPolicy 로 보호되는
    # 배치 전제(spec 권고)이며, 직접 노출 환경에서는 반드시 설정한다.
    webhook_shared_secret: SecretStr = SecretStr("")

    # --- 첨부 다운로드 (FR-002) ---
    # HttpAttachmentDownloader 가 Confluence download_url 바이너리를 저장할 로컬 디렉토리.
    # 다운로더가 local_path 를 채우면 chunk_attachment(파일 직접 읽기)가 처리한다.
    # 실 wiring(자격증명 헤더)은 infra 진입점에서 주입. fixture 는 이미 local_path 보유 → 미사용.
    attachment_download_dir: str = "data/attachments"

    # --- OpenAI ---
    openai_api_key: SecretStr = SecretStr("")
    llm_answer_model: str = "gpt-4o"
    llm_aux_model: str = "gpt-4o-mini"

    # --- 임베딩 / 재순위화 모델 ---
    dense_embedding_model: str = "intfloat/multilingual-e5-large"
    # NOTE: 설계서 §4.5.3 은 ``cross-encoder/ms-marco-MiniLM-L-12`` 로 표기되어 있으나
    # Hugging Face / sentence-transformers 의 실 모델명은 ``-v2`` 가 정식이다.
    # feature17c-10 실험(2026-05-20): 다국어 ``BAAI/bge-reranker-v2-m3`` 로 교체 시
    # 한국어 변별력이 극적으로 개선됨(--debug-rerank: EVAL-032 정답 페이지 #13→#1).
    # 그러나 560M 모델이라 CPU 추론이 느려 기획서 KPI #4(응답 P95 최소 8초/목표 5초)를
    # 위반(재순위는 답변 생성 전 단계라 SSE 스트리밍으로 가려지지 않음). 또한 Precision@3
    # 는 ms-marco + payload 풀텍스트(feature17c-7)만으로 이미 80%(목표 75% 충족)라
    # bge 교체는 선택적 고도화였음. → **지연 KPI 우선으로 ms-marco 로 원복**(feature17c-12).
    # bge 는 운영 GPU 환경(EKS)에서 재검토 — RAG_CROSS_ENCODER_MODEL / _DEVICE 로 전환.
    cross_encoder_model: str = "cross-encoder/ms-marco-MiniLM-L-12-v2"
    # Cross-Encoder Sigmoid temperature scaling (feature17c-1/2).
    # ms-marco 계열은 관련 passage 에 큰 양수 logit(8~11)을 출력해 sigmoid(logit) 가
    # 1.0 으로 saturate → Source.score 변별력 손실. ``sigmoid(logit / temperature)`` 로
    # 분포를 펴 변별력을 회복한다. 운영 logit 분포(--debug-rerank) 상한 ~8.5~8.8 기준
    # T=4.0 채택(강관련 88~90 / 중관련 ~77 / 무관 ~51). select_reranked(LOW 0.55 /
    # NARROW 0.65) / formatter(LOW_CONFIDENCE_SCORE 55) / extract_golden_set(0.80)
    # 임계값이 T=4 기준으로 정합. 다른 T 는 .env(RAG_CROSS_ENCODER_TEMPERATURE)로 override.
    cross_encoder_temperature: float = 4.0
    # Cross-Encoder 추론 장치 (feature17c-11). None(기본)이면 sentence-transformers 가
    # 자동 선택한다. macOS 에서 bge-reranker-v2-m3(560M) 같은 큰 모델은 CPU 자동 선택 시
    # 50건 평가가 매우 느리므로, Apple Silicon 은 ``mps``, NVIDIA 는 ``cuda`` 로 가속할 수
    # 있다. MPS 미지원 연산이 있으면 일부 fallback 이 발생할 수 있으니 문제 시 ``cpu``.
    cross_encoder_device: str | None = None

    # 생성기 환각 보수성 guard (feature17c-14, opt-in). True 면 build_real_deps 가
    # OpenAI transport 에 CONSERVATIVE_SYSTEM_GUARD(미근거 문장 억제 지침)를 주입해
    # 합쳐진 system 메시지 끝에 덧붙인다. 생성기 시스템 프롬프트는 vendoring 안에
    # 하드코딩돼 외부 주입 seam 이 없어 transport 어댑터 경계에서 보강한다(vendoring
    # 무수정). False(기본)는 기존 동작 무변 — A/B 측정용으로 .env 로 토글한다
    # (RAG_GENERATOR_CONSERVATIVE_GUARD=true). 효과는 not_supported_ratio_answerable
    # (feature17c-13)로 측정. 과도 시 답변 완성도(ROUGE-L/BERTScore) 하락 가능.
    generator_conservative_guard: bool = False

    # 생성기 문장별 인용 구조 강제 (feature17c-25, opt-in). True 면 build_real_deps 가
    # OpenAI transport 에 Structured Outputs(json_schema, strict) 스키마
    # (GROUNDED_CITATION_RESPONSE_FORMAT)를 주입해 sentences[].citations 를 문장마다 필수
    # 배열로 강제하고 다중 인용을 description 으로 유도한다(vendoring 무수정, transport 경계).
    # 환각 KPI 잔여 원인 = 다중 청크 종합 문장의 단일 인용(citation 정밀도) 교정 목적. 프롬프트
    # 텍스트 개입(17c-22/23)이 효과 0 으로 실패해 구조적 강제로 전환. False(기본)는 기존
    # json_object 동작 무변 — A/B: RAG_GENERATOR_FORCE_CITATION_SCHEMA=true 로 토글 후
    # per-cited-chunk 환각(not_supported_ratio_answerable/delivered) 재평가로 효과 확인.
    generator_force_citation_schema: bool = False

    # 검증 2단계 전체 top-k grounding 토글 (feature17c-19, opt-in). True 면 의심 문장을
    # 인용 청크가 아니라 검색된 전체 top-k 근거로 2단계 평가한다. 진단(feature17c-18)에서
    # delivered NOT_SUPPORTED 12/12 가 전체 top-k 재평가 시 SUPPORTED 로 뒤집힘(=사실은
    # 검색 근거에 있으나 생성기가 단일 청크만 인용한 citation 정밀도 문제, true 환각 아님)을
    # 확인 → 환각/차단을 "어느 retrieved 근거로도 미지원"으로만 판정하도록 교정. citation
    # 정밀도는 별도 관심사. 검증·차단(공개) 동작 변경이라 기본 OFF, .env 로 A/B
    # (RAG_VERIFIER_FULL_CONTEXT_GROUNDING=true) 후 leniency 검증(--debug-leniency)하고 채택.
    verifier_full_context_grounding: bool = False

    # --- 운영 어댑터 토글 (build_real_deps 후속, 2026-05-18) ---
    # True면 lifespan이 build_real_deps 분기로 E5 + BM25 + Qdrant from_settings +
    # CrossEncoderRerankerImpl 부트스트랩. False(기본)는 build_poc_deps 분기로
    # :memory: Qdrant + Fake everything + samples 자동 인덱싱. 운영 모드는 모델
    # 다운로드(약 2.4 GB)와 Qdrant 서버 접속을 요구하므로 명시 활성화한다.
    use_real_adapters: bool = False


@lru_cache
def get_settings() -> Settings:
    """프로세스 단일 Settings 인스턴스를 반환한다."""
    return Settings()
