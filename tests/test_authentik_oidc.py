"""Native Authentik browser-login contract for the Ark CPA deployment."""

from __future__ import annotations

import pytest

from app.services.authentik_oidc import (
    OidcConfig,
    build_authorization_url,
    create_oidc_attempt,
    get_oidc_config,
    is_oidc_enabled,
)

ENV = {
    "AUTHENTIK_OIDC_ENABLED": "true",
    "AUTHENTIK_OIDC_ISSUER": "https://auth.example/application/o/arkcpa/",
    "AUTHENTIK_OIDC_CLIENT_ID": "arkcpa-client",
    "AUTHENTIK_OIDC_CLIENT_SECRET": "secret",
    "AUTHENTIK_OIDC_REDIRECT_URI": "https://books.example/api/auth/authentik/callback",
    "AUTHENTIK_OIDC_REQUIRED_GROUP": "ARK Family",
}


def test_oidc_config_is_fail_closed_and_https_only():
    assert is_oidc_enabled(ENV)
    assert not is_oidc_enabled({**ENV, "AUTHENTIK_OIDC_CLIENT_SECRET": ""})
    with pytest.raises(ValueError):
        get_oidc_config({**ENV, "AUTHENTIK_OIDC_ISSUER": "http://auth.example/"})


def test_authorization_url_uses_state_nonce_and_s256_pkce():
    config = get_oidc_config(ENV)
    attempt = create_oidc_attempt()
    url = build_authorization_url(
        {
            "authorization_endpoint": "https://auth.example/application/o/authorize/",
            "issuer": config.issuer,
            "token_endpoint": "https://auth.example/token/",
            "jwks_uri": "https://auth.example/jwks/",
        },
        config,
        attempt,
    )
    assert "response_type=code" in url
    assert "code_challenge_method=S256" in url
    assert f"state={attempt.state}" in url
    assert f"nonce={attempt.nonce}" in url
    assert attempt.verifier not in url


def _enable_oidc(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)


def test_status_advertises_authentik_and_hides_public_password(
    unauthed_client, monkeypatch
):
    _enable_oidc(monkeypatch)
    response = unauthed_client.get(
        "/api/auth/status", headers={"host": "books.example"}
    )
    assert response.status_code == 200
    assert response.json()["authentik_enabled"] is True
    assert response.json()["local_password_login"] is False


def test_public_password_login_is_rejected_when_authentik_is_enabled(
    unauthed_client, monkeypatch
):
    _enable_oidc(monkeypatch)
    response = unauthed_client.post(
        "/api/auth/login",
        headers={"host": "books.example"},
        json={"password": "anything-long-enough"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Use Authentik to sign in"


def test_public_first_run_setup_is_rejected_when_authentik_is_enabled(
    unauthed_client, monkeypatch
):
    _enable_oidc(monkeypatch)
    response = unauthed_client.post(
        "/api/auth/setup",
        headers={"host": "books.example"},
        json={"password": "anything-long-enough"},
    )
    assert response.status_code == 403
    assert "loopback" in response.json()["detail"].lower()


def test_callback_rejects_missing_or_mismatched_state(unauthed_client, monkeypatch):
    _enable_oidc(monkeypatch)
    response = unauthed_client.get(
        "/api/auth/authentik/callback?code=fake&state=attacker",
        headers={"host": "books.example", "x-forwarded-proto": "https"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"].endswith("/?auth_error=invalid_state")
    assert "no-store" in response.headers["cache-control"]


def test_authentik_start_sets_host_only_lax_attempt_cookies(
    unauthed_client, monkeypatch
):
    _enable_oidc(monkeypatch)
    config = get_oidc_config(ENV)

    async def fake_discovery(_config: OidcConfig):
        assert _config == config
        return {
            "issuer": config.issuer,
            "authorization_endpoint": "https://auth.example/application/o/authorize/",
            "token_endpoint": "https://auth.example/application/o/token/",
            "jwks_uri": "https://auth.example/application/o/arkcpa/jwks/",
        }

    monkeypatch.setattr("app.routes.auth.discover_oidc", fake_discovery)
    response = unauthed_client.get(
        "/api/auth/authentik",
        headers={"host": "books.example", "x-forwarded-proto": "https"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"].startswith(
        "https://auth.example/application/o/authorize/?"
    )
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 4
    assert all("HttpOnly" in item and "SameSite=lax" in item for item in cookies)
    assert all("Secure" in item for item in cookies)
    assert any(item.startswith("__Host-arkcpa-oidc-state=") for item in cookies)


def test_valid_callback_issues_normal_slowbooks_admin_session(
    unauthed_client, monkeypatch
):
    _enable_oidc(monkeypatch)
    # Local break-glass setup materializes the admin that OIDC maps to.
    setup = unauthed_client.post(
        "/api/auth/setup",
        headers={"host": "testserver"},
        json={"password": "local-recovery-password"},
    )
    assert setup.status_code == 200
    unauthed_client.post("/api/auth/logout")

    config = get_oidc_config(ENV)
    discovery = {
        "issuer": config.issuer,
        "authorization_endpoint": "https://auth.example/application/o/authorize/",
        "token_endpoint": "https://auth.example/application/o/token/",
        "jwks_uri": "https://auth.example/application/o/arkcpa/jwks/",
    }

    async def fake_discovery(_config):
        return discovery

    async def fake_exchange(code, verifier, _discovery, _config):
        assert code == "one-time-code"
        assert verifier
        return "signed-id-token"

    async def fake_verify(token, expected_nonce, _discovery, _config):
        assert token == "signed-id-token"
        assert expected_nonce
        return {
            "sub": "authentik-user-id",
            "email": "owner@example.com",
            "email_verified": True,
            "groups": ["ARK Family"],
        }

    monkeypatch.setattr("app.routes.auth.discover_oidc", fake_discovery)
    monkeypatch.setattr("app.routes.auth.exchange_authorization_code", fake_exchange)
    monkeypatch.setattr("app.routes.auth.verify_id_token", fake_verify)

    started = unauthed_client.get(
        "/api/auth/authentik", headers={"host": "testserver"}, follow_redirects=False
    )
    assert started.status_code == 302
    state = unauthed_client.cookies.get("arkcpa-oidc-state")
    callback = unauthed_client.get(
        f"/api/auth/authentik/callback?code=one-time-code&state={state}",
        headers={"host": "testserver"},
        follow_redirects=False,
    )
    assert callback.status_code == 302
    assert callback.headers["location"] == "https://books.example/"
    status_response = unauthed_client.get(
        "/api/auth/status", headers={"host": "testserver"}
    ).json()
    assert status_response["authenticated"] is True
    assert status_response["user"]["role"] == "admin"
