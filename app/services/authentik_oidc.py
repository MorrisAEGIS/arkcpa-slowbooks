"""Small, fail-closed Authentik OpenID Connect client for browser login."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str
    required_group: str


@dataclass(frozen=True)
class OidcAttempt:
    state: str
    nonce: str
    verifier: str
    challenge: str


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _required(env: dict[str, str] | os._Environ[str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required for Authentik OIDC")
    return value


def _normalize_issuer(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("AUTHENTIK_OIDC_ISSUER must be an HTTPS URL")
    return value.rstrip("/") + "/"


def get_oidc_config(
    env: dict[str, str] | os._Environ[str] = os.environ,
) -> OidcConfig:
    issuer = _normalize_issuer(_required(env, "AUTHENTIK_OIDC_ISSUER"))
    redirect_uri = _required(env, "AUTHENTIK_OIDC_REDIRECT_URI")
    redirect = urlparse(redirect_uri)
    if redirect.scheme != "https" and redirect.hostname not in {
        "127.0.0.1",
        "localhost",
    }:
        raise ValueError("AUTHENTIK_OIDC_REDIRECT_URI must use HTTPS outside loopback")
    return OidcConfig(
        issuer=issuer,
        client_id=_required(env, "AUTHENTIK_OIDC_CLIENT_ID"),
        client_secret=_required(env, "AUTHENTIK_OIDC_CLIENT_SECRET"),
        redirect_uri=redirect_uri,
        scopes=(env.get("AUTHENTIK_OIDC_SCOPES") or "openid profile email").strip(),
        required_group=(env.get("AUTHENTIK_OIDC_REQUIRED_GROUP") or "").strip(),
    )


def is_oidc_enabled(env: dict[str, str] | os._Environ[str] = os.environ) -> bool:
    if not _enabled(env.get("AUTHENTIK_OIDC_ENABLED")):
        return False
    try:
        get_oidc_config(env)
    except ValueError:
        return False
    return True


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def create_oidc_attempt() -> OidcAttempt:
    verifier = _base64url(secrets.token_bytes(48))
    return OidcAttempt(
        state=_base64url(secrets.token_bytes(32)),
        nonce=_base64url(secrets.token_bytes(32)),
        verifier=verifier,
        challenge=_base64url(hashlib.sha256(verifier.encode("ascii")).digest()),
    )


def _provider_url(value: Any, issuer: str, field: str) -> str:
    candidate = str(value or "")
    parsed = urlparse(candidate)
    expected = urlparse(issuer)
    if parsed.scheme != "https" or parsed.netloc != expected.netloc:
        raise ValueError(f"OIDC discovery returned an unsafe {field}")
    return candidate


async def discover_oidc(config: OidcConfig) -> dict[str, str]:
    discovery_url = config.issuer + ".well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            discovery_url, headers={"accept": "application/json"}
        )
        response.raise_for_status()
    raw = response.json()
    if _normalize_issuer(str(raw.get("issuer") or "")) != config.issuer:
        raise ValueError("OIDC issuer mismatch")
    return {
        "issuer": config.issuer,
        "authorization_endpoint": _provider_url(
            raw.get("authorization_endpoint"), config.issuer, "authorization endpoint"
        ),
        "token_endpoint": _provider_url(
            raw.get("token_endpoint"), config.issuer, "token endpoint"
        ),
        "jwks_uri": _provider_url(raw.get("jwks_uri"), config.issuer, "JWKS endpoint"),
    }


def build_authorization_url(
    discovery: dict[str, str], config: OidcConfig, attempt: OidcAttempt
) -> str:
    query = urlencode(
        {
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "response_type": "code",
            "scope": config.scopes,
            "state": attempt.state,
            "nonce": attempt.nonce,
            "code_challenge": attempt.challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{discovery['authorization_endpoint']}?{query}"


async def exchange_authorization_code(
    code: str,
    verifier: str,
    discovery: dict[str, str],
    config: OidcConfig,
) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            discovery["token_endpoint"],
            auth=(config.client_id, config.client_secret),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirect_uri,
                "code_verifier": verifier,
            },
            headers={"accept": "application/json"},
        )
        response.raise_for_status()
    id_token = response.json().get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise ValueError("Authentik token response did not include an ID token")
    return id_token


async def verify_id_token(
    id_token: str,
    expected_nonce: str,
    discovery: dict[str, str],
    config: OidcConfig,
) -> dict[str, Any]:
    header = jwt.get_unverified_header(id_token)
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise ValueError("Authentik ID token uses an unsupported signing algorithm")

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            discovery["jwks_uri"], headers={"accept": "application/json"}
        )
        response.raise_for_status()
    keys = response.json().get("keys") or []
    jwk = next(
        (
            candidate
            for candidate in keys
            if candidate.get("kid") == header["kid"] and candidate.get("kty") == "RSA"
        ),
        None,
    )
    if jwk is None:
        raise ValueError("Authentik signing key was not found")

    claims = jwt.decode(
        id_token,
        key=RSAAlgorithm.from_jwk(jwk),
        algorithms=["RS256"],
        audience=config.client_id,
        issuer=config.issuer,
        options={"require": ["exp", "iss", "aud", "sub", "nonce", "email"]},
    )
    if not secrets.compare_digest(str(claims.get("nonce") or ""), expected_nonce):
        raise ValueError("Authentik ID token nonce mismatch")
    if claims.get("email_verified") is not True:
        raise ValueError("Authentik identity email is not verified")
    groups = claims.get("groups") or []
    if not isinstance(groups, list):
        groups = []
    if config.required_group and config.required_group not in groups:
        raise PermissionError("Authentik identity is not in the required group")
    return claims
