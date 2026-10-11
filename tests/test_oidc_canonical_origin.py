"""OIDC attempts must start on the configured callback origin."""

from urllib.parse import parse_qs, urlsplit

import pytest

from app.routes import auth

CANONICAL = "https://cpa.enerteqpower.com"


@pytest.fixture
def oidc(monkeypatch):
    for key, value in {
        "AUTHENTIK_OIDC_ENABLED": "true",
        "AUTHENTIK_OIDC_ISSUER": "https://auth.example/application/o/arkcpa/",
        "AUTHENTIK_OIDC_CLIENT_ID": "synthetic-client",
        "AUTHENTIK_OIDC_CLIENT_SECRET": "synthetic-test-secret",
        "AUTHENTIK_OIDC_REDIRECT_URI": CANONICAL + "/api/auth/authentik/callback",
    }.items():
        monkeypatch.setenv(key, value)


def forbid_attempt(monkeypatch):
    async def no_discovery(_config):
        pytest.fail("noncanonical request contacted the identity provider")

    def no_attempt():
        pytest.fail("noncanonical request created an OIDC attempt")

    monkeypatch.setattr(auth, "discover_oidc", no_discovery)
    monkeypatch.setattr(auth, "create_oidc_attempt", no_attempt)


@pytest.mark.parametrize(
    "origin",
    [
        "https://cpa.magaenergy.ai",
        "https://cpa.arkteq.ai",
        "http://cpa.enerteqpower.com",
        "https://cpa.enerteqpower.com:8443",
        "http://localhost",
        "http://testserver",
    ],
)
def test_alias_redirects_before_discovery_or_cookies(
    unauthed_client, monkeypatch, oidc, origin
):
    forbid_attempt(monkeypatch)
    response = unauthed_client.get(
        origin + "/api/auth/authentik?next=%2Freports&untrusted=value",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == (
        CANONICAL + "/api/auth/authentik?next=%2Freports"
    )
    assert not response.headers.get_list("set-cookie")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("next_path", ["//evil.example", "https://evil.example", ""])
def test_unsafe_return_path_cannot_change_canonical_destination(
    unauthed_client, monkeypatch, oidc, next_path
):
    forbid_attempt(monkeypatch)
    response = unauthed_client.get(
        "https://cpa.magaenergy.ai/api/auth/authentik",
        params={"next": next_path},
        headers={"x-forwarded-host": "evil.example"},
        follow_redirects=False,
    )
    assert response.headers["location"] == CANONICAL + "/api/auth/authentik?next=%2F"
    assert not response.headers.get_list("set-cookie")


def test_forwarded_host_cannot_suppress_canonical_redirect(
    unauthed_client, monkeypatch, oidc
):
    forbid_attempt(monkeypatch)
    response = unauthed_client.get(
        "https://evil.example/api/auth/authentik",
        headers={"x-forwarded-host": "cpa.enerteqpower.com"},
        follow_redirects=False,
    )
    assert response.headers["location"] == CANONICAL + "/api/auth/authentik?next=%2F"
    assert not response.headers.get_list("set-cookie")


def test_safe_return_query_is_encoded_as_one_parameter(
    unauthed_client, monkeypatch, oidc
):
    forbid_attempt(monkeypatch)
    next_path = "/reports?filter=one&next=//evil.example#section"
    response = unauthed_client.get(
        "https://cpa.magaenergy.ai/api/auth/authentik",
        params={"next": next_path},
        follow_redirects=False,
    )
    destination = urlsplit(response.headers["location"])
    assert destination.netloc == "cpa.enerteqpower.com"
    assert destination.path == "/api/auth/authentik"
    assert destination.fragment == ""
    assert parse_qs(destination.query) == {"next": [next_path]}


@pytest.mark.parametrize(
    "origin,headers",
    [
        (CANONICAL, {}),
        (CANONICAL + ":443", {}),
        ("http://cpa.enerteqpower.com", {"x-forwarded-proto": "https"}),
    ],
)
def test_canonical_origin_starts_pkce_with_host_only_cookies(
    unauthed_client, monkeypatch, oidc, origin, headers
):
    async def discovery(config):
        return {
            "issuer": config.issuer,
            "authorization_endpoint": "https://auth.example/application/o/authorize/",
            "token_endpoint": "https://auth.example/application/o/token/",
            "jwks_uri": "https://auth.example/application/o/arkcpa/jwks/",
        }

    monkeypatch.setattr(auth, "discover_oidc", discovery)
    response = unauthed_client.get(
        origin + "/api/auth/authentik", headers=headers, follow_redirects=False
    )
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert location.netloc == "auth.example"
    params = parse_qs(location.query)
    assert params["redirect_uri"] == [CANONICAL + "/api/auth/authentik/callback"]
    assert params["code_challenge_method"] == ["S256"]
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 4
    assert all("Secure" in value and "HttpOnly" in value for value in cookies)
    assert all("SameSite=lax" in value and "Domain=" not in value for value in cookies)
    assert any(value.startswith("__Host-arkcpa-oidc-state=") for value in cookies)


def test_disabled_oidc_keeps_unavailable_response(unauthed_client, monkeypatch, oidc):
    monkeypatch.setenv("AUTHENTIK_OIDC_ENABLED", "false")
    forbid_attempt(monkeypatch)
    response = unauthed_client.get(
        "https://cpa.magaenergy.ai/api/auth/authentik", follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["location"] == CANONICAL + "/?auth_error=unavailable"
    assert response.headers["cache-control"] == "no-store"
    assert all(
        "Max-Age=0" in value for value in response.headers.get_list("set-cookie")
    )


def test_configured_loopback_origin_can_start_its_own_attempt(
    unauthed_client, monkeypatch, oidc
):
    callback = "http://localhost:3001/api/auth/authentik/callback"
    monkeypatch.setenv("AUTHENTIK_OIDC_REDIRECT_URI", callback)

    async def discovery(config):
        return {
            "issuer": config.issuer,
            "authorization_endpoint": "https://auth.example/application/o/authorize/",
            "token_endpoint": "https://auth.example/application/o/token/",
            "jwks_uri": "https://auth.example/application/o/arkcpa/jwks/",
        }

    monkeypatch.setattr(auth, "discover_oidc", discovery)
    response = unauthed_client.get(
        "http://localhost:3001/api/auth/authentik", follow_redirects=False
    )
    assert response.status_code == 302
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["redirect_uri"] == [callback]
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 4
    assert any(value.startswith("arkcpa-oidc-state=") for value in cookies)
    assert all("Domain=" not in value for value in cookies)
