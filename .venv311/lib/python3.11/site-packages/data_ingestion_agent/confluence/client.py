from __future__ import annotations

"""
--------------------------------------------------
작성자 : Codex
작성목적 : Confluence API 호출을 담당하는 client/transport 계층 구현.
          feature2 범위에서 pagination, retry/backoff, 오류 분류를 제공한다.
작성일 : 2026-05-14
변경사항 내역 (날짜, 변경목적, 변경내용 순)
  - 2026-05-14, 최초 작성, Confluence client와 fakeable transport 인터페이스 구현
--------------------------------------------------
[호환성]
  - Python 3.11.x 권장
  - 표준 라이브러리 urllib 기반 기본 transport
--------------------------------------------------
"""

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from data_ingestion_agent.config import DataIngestionConfig

DEFAULT_PAGE_LIMIT = 25
CONFLUENCE_API_ORIGIN = "https://api.atlassian.com"


@dataclass(frozen=True, slots=True)
class ConfluenceRequest:
    """Transport가 수행할 Confluence HTTP request."""

    method: str
    url: str
    headers: dict[str, str] = field(repr=False)
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ConfluenceResponse:
    """Transport가 반환하는 최소 HTTP response."""

    status_code: int
    json_body: dict[str, Any]


class ConfluenceTransport(Protocol):
    """Confluence request를 수행하는 transport protocol."""

    def send(self, request: ConfluenceRequest) -> ConfluenceResponse:
        """HTTP request를 수행하고 response를 반환한다."""


class ConfluenceApiError(RuntimeError):
    """Confluence API 실패를 안전하게 표현하는 예외.

    Args:
        status_code: HTTP status code. Timeout 등 transport 실패는 None.
        error_type: auth_failure, permission_failure 등 안전한 오류 분류.
        message: 민감값을 제거한 오류 설명.
        retryable: retry/backoff 대상 여부.
        item_level: Page/Space item failure로 전환 가능한 오류 여부.
        attempt_count: 실제 시도 횟수.
    """

    def __init__(
        self,
        *,
        status_code: int | None,
        error_type: str,
        message: str,
        retryable: bool,
        item_level: bool,
        attempt_count: int,
    ) -> None:
        self.status_code = status_code
        self.error_type = error_type
        self.retryable = retryable
        self.item_level = item_level
        self.attempt_count = attempt_count
        super().__init__(
            "Confluence API request failed "
            f"(status_code={status_code}, error_type={error_type}, "
            f"retryable={retryable}, item_level={item_level}, "
            f"attempt_count={attempt_count}): {message}"
        )


class UrllibConfluenceTransport:
    """urllib 기반 기본 transport.

    실제 네트워크 호출은 feature2 테스트에서 사용하지 않고, 운영 실행 시 client의
    기본 transport로만 제공한다.
    """

    def send(self, request: ConfluenceRequest) -> ConfluenceResponse:
        """Confluence HTTP request를 수행한다.

        Args:
            request: method, url, headers, timeout을 포함한 transport request.

        Returns:
            JSON body를 포함한 ConfluenceResponse.

        Raises:
            TimeoutError: 네트워크 timeout으로 분류할 수 있는 경우.
        """
        urllib_request = Request(
            request.url,
            headers=request.headers,
            method=request.method,
        )
        try:
            with urlopen(urllib_request, timeout=request.timeout_seconds) as response:
                body = response.read().decode("utf-8")
                return ConfluenceResponse(
                    status_code=response.status,
                    json_body=json.loads(body) if body else {},
                )
        except HTTPError as error:
            body = error.read().decode("utf-8")
            return ConfluenceResponse(
                status_code=error.code,
                json_body=json.loads(body) if body else {},
            )
        except TimeoutError:
            raise
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise error.reason
            raise TimeoutError("Confluence request failed before response") from error


class ConfluenceClient:
    """Confluence API v2 client.

    Space 목록, descendants page tree, page detail 조회와 `_links.next` 기반
    cursor pagination을 담당한다. HTML 변환과 processed document mapping은 후속
    feature의 책임으로 남긴다.
    """

    def __init__(
        self,
        *,
        config: DataIngestionConfig,
        transport: ConfluenceTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibConfluenceTransport()
        self.sleeper = sleeper
        if config.use_admin_key:
            self.base_url = f"{config.site_url.rstrip('/')}/wiki/api/v2"
        else:
            self.base_url = f"{CONFLUENCE_API_ORIGIN}/ex/confluence/{config.cloud_id}/wiki/api/v2"

    def list_spaces(self) -> list[dict[str, Any]]:
        """접근 가능한 Confluence Space 목록을 pagination 처리해 반환한다."""
        return self._get_paginated("/spaces", {"limit": DEFAULT_PAGE_LIMIT})

    def list_page_descendants(self, homepage_id: str) -> list[dict[str, Any]]:
        """homepage 기준 descendants page refs를 pagination 처리해 반환한다."""
        if not homepage_id:
            raise ValueError("homepage_id is required")
        return self._get_paginated(
            f"/pages/{homepage_id}/descendants",
            {"limit": DEFAULT_PAGE_LIMIT},
        )

    def get_page_detail(self, page_id: str) -> dict[str, Any]:
        """Page 상세를 storage body와 version 포함 형태로 조회한다."""
        if not page_id:
            raise ValueError("page_id is required")
        response_body = self._request_json(
            f"/pages/{page_id}",
            {
                "body-format": "storage",
                "include-version": "true",
            },
        )
        return response_body

    def get_page_read_restrictions(self, page_id: str) -> dict[str, Any]:
        """Page read restriction을 조회한다.

        Confluence v2 page 조회 응답에는 권한 정보가 직접 포함되지 않는다. 운영 ACL
        payload(`allowed_users`/`allowed_groups`)를 만들기 위해 v1 content restriction
        endpoint를 별도로 호출한다.
        """
        if not page_id:
            raise ValueError("page_id is required")
        return self._request_json(f"/rest/api/content/{page_id}/restriction/byOperation/read")

    def _get_paginated(
        self,
        path: str,
        query: dict[str, str | int],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_path_or_url: str | None = self._build_path_with_query(path, query)

        while next_path_or_url:
            response_body = self._request_json(next_path_or_url)
            results = response_body.get("results", [])
            if not isinstance(results, list):
                raise ConfluenceApiError(
                    status_code=None,
                    error_type="invalid_response",
                    message="Paginated response results must be a list",
                    retryable=False,
                    item_level=False,
                    attempt_count=1,
                )
            items.extend(results)
            next_path_or_url = response_body.get("_links", {}).get("next")

        return items

    def _request_json(
        self,
        path_or_url: str,
        query: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        url = self._build_url(path_or_url, query)
        attempt_count = 0
        last_retryable_response: ConfluenceResponse | None = None

        while attempt_count <= self.config.max_retries:
            attempt_count += 1
            request = ConfluenceRequest(
                method="GET",
                url=url,
                headers=self._headers(),
                timeout_seconds=self.config.timeout_seconds,
            )
            try:
                response = self.transport.send(request)
            except TimeoutError:
                if attempt_count > self.config.max_retries:
                    raise ConfluenceApiError(
                        status_code=None,
                        error_type="retry_exhausted",
                        message="Confluence request timed out after retries",
                        retryable=True,
                        item_level=False,
                        attempt_count=attempt_count,
                    ) from None
                self._sleep_before_retry(attempt_count)
                continue

            if 200 <= response.status_code < 300:
                return response.json_body

            error_classification = self._classify_response(response.status_code)
            if not error_classification.retryable:
                raise self._api_error(
                    response=response,
                    error_type=error_classification.error_type,
                    retryable=False,
                    item_level=error_classification.item_level,
                    attempt_count=attempt_count,
                )

            last_retryable_response = response
            if attempt_count > self.config.max_retries:
                break
            self._sleep_before_retry(attempt_count)

        if last_retryable_response is None:
            raise ConfluenceApiError(
                status_code=None,
                error_type="retry_exhausted",
                message="Confluence request exhausted retries",
                retryable=True,
                item_level=False,
                attempt_count=attempt_count,
            )

        raise self._api_error(
            response=last_retryable_response,
            error_type="retry_exhausted",
            retryable=True,
            item_level=False,
            attempt_count=attempt_count,
        )

    def _headers(self) -> dict[str, str]:
        if self.config.use_admin_key:
            raw = f"{self.config.admin_email}:{self.config.admin_api_token}".encode()
            return {
                "Accept": "application/json",
                "Authorization": f"Basic {base64.b64encode(raw).decode('ascii')}",
                "Atl-Confluence-With-Admin-Key": "true",
            }
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config.access_token}",
        }

    def _build_url(
        self,
        path_or_url: str,
        query: dict[str, str | int] | None = None,
    ) -> str:
        if path_or_url.startswith("https://"):
            return path_or_url

        path_with_query = self._build_path_with_query(path_or_url, query or {})
        if self.config.use_admin_key:
            site_url = self.config.site_url.rstrip("/")
            if path_with_query.startswith("/wiki/"):
                return f"{site_url}{path_with_query}"
            if path_with_query.startswith("/rest/api/"):
                return f"{site_url}/wiki{path_with_query}"
            return f"{self.base_url}{path_with_query}"
        if path_with_query.startswith("/wiki/api/v2/") or path_with_query.startswith(
            "/wiki/rest/api/"
        ):
            return urljoin(CONFLUENCE_API_ORIGIN, path_with_query)
        if path_with_query.startswith("/rest/api/"):
            return (
                f"{CONFLUENCE_API_ORIGIN}/ex/confluence/{self.config.cloud_id}/wiki"
                f"{path_with_query}"
            )
        return f"{self.base_url}{path_with_query}"

    @staticmethod
    def _build_path_with_query(
        path: str,
        query: dict[str, str | int],
    ) -> str:
        if not query:
            return path
        return f"{path}?{urlencode(query)}"

    def _sleep_before_retry(self, attempt_count: int) -> None:
        delay_seconds = self.config.request_delay_seconds * attempt_count
        if delay_seconds > 0:
            self.sleeper(delay_seconds)

    def _api_error(
        self,
        *,
        response: ConfluenceResponse,
        error_type: str,
        retryable: bool,
        item_level: bool,
        attempt_count: int,
    ) -> ConfluenceApiError:
        return ConfluenceApiError(
            status_code=response.status_code,
            error_type=error_type,
            message=self._safe_error_message(response),
            retryable=retryable,
            item_level=item_level,
            attempt_count=attempt_count,
        )

    def _safe_error_message(self, response: ConfluenceResponse) -> str:
        raw_message = response.json_body.get("message")
        if not isinstance(raw_message, str) or not raw_message:
            return "Confluence API returned an error"
        return raw_message.replace(self.config.access_token, "<redacted>")

    @staticmethod
    def _classify_response(status_code: int) -> "_ErrorClassification":
        if status_code == 401:
            return _ErrorClassification(
                error_type="auth_failure",
                retryable=False,
                item_level=False,
            )
        if status_code == 403:
            return _ErrorClassification(
                error_type="permission_failure",
                retryable=False,
                item_level=True,
            )
        if status_code == 404:
            return _ErrorClassification(
                error_type="item_not_found",
                retryable=False,
                item_level=True,
            )
        if status_code == 429 or status_code >= 500:
            return _ErrorClassification(
                error_type="retryable_http_error",
                retryable=True,
                item_level=False,
            )
        if status_code == 400:
            return _ErrorClassification(
                error_type="bad_request",
                retryable=False,
                item_level=False,
            )
        return _ErrorClassification(
            error_type="http_error",
            retryable=False,
            item_level=False,
        )


@dataclass(frozen=True, slots=True)
class _ErrorClassification:
    error_type: str
    retryable: bool
    item_level: bool
