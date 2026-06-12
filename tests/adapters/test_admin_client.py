"""admin-key credential 경로(Basic + site URL) 테스트 — api-spec v2.6.1 §1-4 ④⑤.

작성자 : 최태성
담당 영역 : ingestion

Feature 0 게이트 결론(2026-06-11): admin-key 는 OAuth Bearer/게이트웨이 경로에서 동작하지
않는다. 본 테스트는 (1) Basic 인증 헤더 산출, (2) vendored ConfluenceClient 무수정
서브클래스의 site URL 라우팅, (3) 필수 admin credential fail-fast, (4) factory 의
admin 경로 자격증명 검증 계약을 고정한다 — Bearer 회귀 시 restricted 페이지가 무음
누락되므로(api-spec v2.6.1 주의) 헤더 계약을 테스트로 못 박는다.
"""

from __future__ import annotations

import base64

import pytest

from app.adapters.atlassian import (
    _build_admin_confluence_client,
    build_admin_basic_authorization,
    build_restriction_acl_provider,
)
from app.adapters.factory import MissingAtlassianCredentialsError, build_source_adapter
from app.config import Settings


def _admin_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "use_real_adapters": False,
        "source_type": "atlassian",
        "atlassian_use_admin_key": True,
        "atlassian_site_url": "https://lina.atlassian.net",
        "atlassian_admin_email": "admin@lina.example",
        "atlassian_admin_api_token": "api-token-001",
        "atlassian_cloud_id": "cloud-001",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_build_admin_basic_authorization_encodes_email_and_token() -> None:
    value = build_admin_basic_authorization("admin@lina.example", "api-token-001")

    assert value.startswith("Basic ")
    decoded = base64.b64decode(value.removeprefix("Basic ")).decode()
    assert decoded == "admin@lina.example:api-token-001"


def test_admin_client_uses_basic_auth_and_admin_key_header_not_bearer() -> None:
    client = _build_admin_confluence_client(_admin_settings())

    headers = client._headers()
    assert headers["Authorization"].startswith("Basic ")
    assert "Bearer" not in headers["Authorization"]
    assert headers["Atl-Confluence-With-Admin-Key"] == "true"


def test_admin_client_routes_to_site_url_not_gateway() -> None:
    # admin-key 적용 read 는 site URL 에서만 동작한다 — 게이트웨이(api.atlassian.com)
    # 경로로 새면 restricted 페이지가 무음 누락된다(api-spec v2.6.1 주의).
    client = _build_admin_confluence_client(_admin_settings())

    assert client.base_url == "https://lina.atlassian.net/wiki/api/v2"
    # 상대 경로(v2 API) → site 기준.
    assert client._build_url("/pages/1") == "https://lina.atlassian.net/wiki/api/v2/pages/1"
    # /wiki 절대 경로 → site 직결.
    assert (
        client._build_url("/wiki/rest/api/content/1")
        == "https://lina.atlassian.net/wiki/rest/api/content/1"
    )
    # v1 REST 경로 → site/wiki 프리픽스.
    assert (
        client._build_url("/rest/api/content/1/restriction/byOperation/read")
        == "https://lina.atlassian.net/wiki/rest/api/content/1/restriction/byOperation/read"
    )
    # 절대 URL 은 그대로 통과(페이지네이션 next 링크).
    assert client._build_url("https://lina.atlassian.net/wiki/api/v2/pages?cursor=x") == (
        "https://lina.atlassian.net/wiki/api/v2/pages?cursor=x"
    )
    assert "api.atlassian.com" not in client._build_url("/pages/1")


@pytest.mark.parametrize(
    "missing",
    [
        {"atlassian_site_url": ""},
        {"atlassian_admin_email": ""},
        {"atlassian_admin_api_token": ""},
    ],
)
def test_admin_client_requires_admin_credentials(missing: dict[str, object]) -> None:
    with pytest.raises(RuntimeError, match="RAG_ATLASSIAN_SITE_URL"):
        _build_admin_confluence_client(_admin_settings(**missing))


def test_restriction_provider_client_uses_basic_auth() -> None:
    provider = build_restriction_acl_provider(_admin_settings())

    assert provider is not None
    assert provider.client._headers()["Authorization"].startswith("Basic ")


def test_trash_client_admin_mode_uses_basic_auth_and_site_url() -> None:
    from app.adapters.confluence_trash import ConfluenceTrashContentClient

    client = ConfluenceTrashContentClient.from_settings(_admin_settings())

    headers = client._headers()
    assert headers["Authorization"].startswith("Basic ")
    assert headers["Atl-Confluence-With-Admin-Key"] == "true"
    assert client._wiki_base() == "https://lina.atlassian.net/wiki"
    assert client._absolutize("/wiki/rest/api/content?cursor=x") == (
        "https://lina.atlassian.net/wiki/rest/api/content?cursor=x"
    )


def test_trash_client_admin_mode_requires_site_and_authorization() -> None:
    from app.adapters.confluence_trash import ConfluenceTrashContentClient

    with pytest.raises(ValueError, match="site_url/admin_authorization"):
        ConfluenceTrashContentClient(cloud_id="c", access_token="t", use_admin_key=True)


def test_trash_client_oauth_mode_keeps_bearer_gateway() -> None:
    from app.adapters.confluence_trash import ConfluenceTrashContentClient

    client = ConfluenceTrashContentClient.from_settings(
        _admin_settings(atlassian_use_admin_key=False, atlassian_access_token="oauth-token")
    )

    assert client._headers()["Authorization"] == "Bearer oauth-token"
    assert "api.atlassian.com" in client._wiki_base()


def test_factory_admin_path_requires_admin_credentials_not_oauth_token() -> None:
    # admin 경로는 OAuth access_token 없이도 생성된다(v2.6.1 — OAuth 미사용).
    adapter = build_source_adapter(_admin_settings(atlassian_access_token=""))
    assert adapter is not None

    # admin credential 누락은 명확히 거부된다.
    with pytest.raises(MissingAtlassianCredentialsError, match="ADMIN_API_TOKEN"):
        build_source_adapter(_admin_settings(atlassian_admin_api_token=""))
