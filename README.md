# LINA Data Ingestion Pipeline

![Last commit](https://img.shields.io/github/last-commit/skala-final-project-team7/ingestion-deploy?style=flat-square)
![Issues](https://img.shields.io/github/issues/skala-final-project-team7/ingestion-deploy?style=flat-square)
![PRs](https://img.shields.io/github/issues-pr/skala-final-project-team7/ingestion-deploy?style=flat-square)
![Top language](https://img.shields.io/github/languages/top/skala-final-project-team7/ingestion-deploy?style=flat-square)

![Python 3.11](https://img.shields.io/badge/Python_3.11-3776AB?style=flat-square&logo=python&logoColor=white)
![RabbitMQ](https://img.shields.io/badge/RabbitMQ-FF6600?style=flat-square&logo=rabbitmq&logoColor=white)
![MongoDB](https://img.shields.io/badge/MongoDB-47A248?style=flat-square&logo=mongodb&logoColor=white)

척척학사(LINA) Confluence 기반 RAG 챗봇 서비스의 **데이터 수집 파이프라인**.
Confluence 문서·첨부를 수집(Full Crawl / Delta Sync)하여 첨부 텍스트 추출 → Adaptive Chunking →
Dual Embedding 색인까지 수행한다. RAG Pipeline(질의/응답, `../rag`)과 분리된 독립 배포 단위이며,
RabbitMQ 기반 비동기 파이프라인으로 동작한다.

## 구성

| 단계 | FR | 모듈 |
|---|---|---|
| Confluence Full Crawl | FR-001 | `app/ingestion/crawler.py` |
| Delta Sync / 삭제 동기화 | — | `app/ingestion/sync.py` |
| 첨부 텍스트 추출 (PDF/Word/Excel) | FR-002 | `app/ingestion/extractor/` |
| Adaptive Chunking (본문 6 + 첨부 3유형) | FR-003 | `app/ingestion/chunker/` |
| Dual Embedding 색인 (Dense+Sparse, Qdrant Multi-Pool) | FR-004 | `app/ingestion/embedder/`, `embedding.py`, `vector_store.py`, `indexer.py` |
| RabbitMQ Worker | — | `app/ingestion/workers/` |

자세한 흐름은 `docs/architecture.md`, 진행 계획은 `docs/ai/current-plan.md` 참조.

## 실행 · 통합 계약 (인프라 담당자용)

> 컨테이너화·CI·배포는 인프라에서 담당한다. 통합에 필요한 계약을 아래 한 곳에 모은다.

| 항목 | 값 |
|---|---|
| 기동 (HTTP API) | `uvicorn app.api.main:app --host 0.0.0.0 --port 8001` |
| 기동 (**Chunking Worker — 운영 필수**) | `python -m app.ingestion.workers.chunking_main` (또는 콘솔 스크립트 `ingestion-chunking-worker`) — `content.chunking` 큐를 소비해 청킹→임베딩→Qdrant upsert 를 수행한다. **워커가 없으면 운영 모드 수집이 발행만 하고 색인되지 않는다** |
| 엔드포인트 | `POST /ml/ingest` · `GET /ml/ingest/status/{job_id}` · `GET /ml/ingest/health` · `POST /ml/confluence/webhook` · `GET /healthz` · `GET /metrics`(Prometheus) |
| Python | 3.11.x (`>=3.11,<3.12`) |
| 부팅 필수 설치 | `pip install -e ".[ingestion]"` — **base 설치(`pip install -e .`)만으로는 부팅 불가** (import 시 chunker 가 PyMuPDF·python-docx·openpyxl·BeautifulSoup4 를 끌어옴) |
| 운영(real) 모드 | `RAG_USE_REAL_ADAPTERS=true` — **API 가 직접 운영 경로로 분기한다**(2026-06-11 fix): full crawl→Mongo `raw_pages` 적재 + RabbitMQ `content.chunking` 발행, delta→`run_delta_sync` 실 러너, 완료 이벤트→실 RabbitMQ 발행. 워커 추가 설치: `[embedding]`(torch·sentence-transformers 약 2.4GB) · `[agents]`(LangGraph; 미설치 시 sequential fallback) |
| 기동 모드 확인 | 두 프로세스 모두 기동 로그에 모드를 남긴다 — **PoC 모드면 WARNING**("실 Qdrant 에 적재되지 않는다"). 운영 배포 후 이 경고가 보이면 env 누락이다 |
| 외부 의존 | **RabbitMQ(필수)** `RAG_RABBITMQ_URL`(기본 `amqp://guest:guest@localhost:5672/%2F`) · Qdrant · MongoDB · MySQL · OpenAI · Confluence(`RAG_SOURCE_TYPE=atlassian` 모드 — 자격증명은 BFF 가 잡마다 전달하며, Settings 의 `RAG_ATLASSIAN_*` 는 fallback) |
| 환경변수 | `app/config.py`(Settings) 참조 — 시크릿은 `RAG_OPENAI_API_KEY` · `RAG_ATLASSIAN_ACCESS_TOKEN` · `RAG_RABBITMQ_URL`(자격증명 포함) (`.env.example`은 미포함 — 인프라 관리) |
| env 프리픽스 | `RAG_` — **`rag` 레포와 동일 env 네임스페이스를 의도적으로 공유**(같은 Qdrant/Mongo/MySQL 을 가리킴). 두 서비스를 하나의 ConfigMap/`.env` 로 합칠 경우 값이 동일해야 충돌하지 않는다 |
| 인증 · CORS | 본 앱은 미들웨어 없음 — **BFF 가 담당** |
| 헬스 체크 성격 | `/healthz`·`/ml/ingest/health` 는 **liveness 전용**(항상 `UP`/`ok`, 의존성·RabbitMQ 끊김은 보고하지 않음) |

> 공용 백킹 서비스(Qdrant/MongoDB/MySQL)와 RabbitMQ는 **인프라가 제공**한다 (이 레포에 `docker-compose.yml` 미포함).
>
> **운영 토폴로지(최소 2 프로세스):** ① HTTP API(크롤·발행) + ② Chunking Worker(소비·색인 — 독립 스케일링).
> 잡 수명주기 저장소(`InMemoryIngestJobStore`)는 단일 API 인스턴스 전제다(재시작 시 진행 중 잡 상태 유실 — durable 승급은 후속).

## 개발 환경

```bash
python -m venv .venv && source .venv/bin/activate   # Python 3.11
pip install -e ".[ingestion,embedding,dev]"
```

> **부팅 필수 extra:** `pip install -e .` (extra 없이) 만으로는 **앱이 기동되지 않는다**.
> `app.api.main` import 시 chunker 가 PyMuPDF·python-docx·openpyxl·BeautifulSoup4(= `ingestion`
> extra)를 끌어오기 때문이다. 따라서 **최소 `pip install -e ".[ingestion]"`** 이 필요하다.
> `embedding` extra(torch·sentence-transformers, 약 2.4GB)는 lazy 로딩이라 운영(real) 모드에서만
> 필요하다.

## 검증

```bash
./scripts/format.sh   # ruff format
./scripts/lint.sh     # ruff check
./scripts/test.sh     # pytest
./scripts/verify.sh   # format → lint → test
```

## 비고

- `app/schemas`·`app/ingestion/chunker`·`embedder`·`embedding.py`·`vector_store.py`·`indexer.py`·
  `app/adapters`·`app/storage` 는 RAG 레포(`../rag`)에서 복사한 공유 자산이다. 공통 계약(Qdrant payload /
  `embedding_cache` 키 / ACL 필드) 변경 시 RAG 레포와 함께 갱신한다.
- Confluence OAuth access token / cloudId 등 민감 정보는 로그·메시지 페이로드에 남기지 않는다.
