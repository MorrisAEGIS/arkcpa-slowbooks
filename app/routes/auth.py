# ============================================================================
# Slowbooks Pro 2026 — Auth routes
#
#   GET  /api/auth/status  → {setup_needed, authenticated, multi_user, user?}
#   POST /api/auth/setup   → first-time password set (409 if already set)
#   POST /api/auth/login   → password (+ username once 2+ users exist)
#   POST /api/auth/logout  → clear session
#
# These routes are deliberately NOT protected by require_auth — they're
# how you become authenticated in the first place.
# ============================================================================

import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import Field
from app.schemas.common import StrictModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.auth import LoginAttempt
from app.services.auth import (
    authenticate,
    ensure_admin_user,
    is_multi_user,
    password_is_set,
    set_password,
)
from app.services.rate_limit import limiter
from app.services.request_utils import client_ip as _client_ip
from app.services.settings_service import set_setting
from app.services.authentik_oidc import (
    build_authorization_url,
    create_oidc_attempt,
    discover_oidc,
    exchange_authorization_code,
    get_oidc_config,
    is_oidc_enabled,
    verify_id_token,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)

_OIDC_COOKIE_MAX_AGE = 10 * 60
_OIDC_COOKIE_PREFIX = "arkcpa-oidc-"


def _is_secure_request(request: Request) -> bool:
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0]
    return forwarded.strip().lower() == "https" or request.url.scheme == "https"


def _is_loopback_request(request: Request) -> bool:
    forwarded = (request.headers.get("x-forwarded-host") or "").split(",", 1)[0]
    if forwarded.strip():
        host = urlsplit("//" + forwarded.strip()).hostname or ""
    else:
        host = request.url.hostname or ""
    host = host.lower()
    return host in {"127.0.0.1", "localhost", "::1", "testserver"}


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def _cookie_name(name: str, secure: bool) -> str:
    return (
        f"__Host-{_OIDC_COOKIE_PREFIX}{name}"
        if secure
        else f"{_OIDC_COOKIE_PREFIX}{name}"
    )


def _set_attempt_cookie(
    response: RedirectResponse, name: str, value: str, secure: bool
) -> None:
    response.set_cookie(
        _cookie_name(name, secure),
        value,
        max_age=_OIDC_COOKIE_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _clear_attempt_cookies(response: RedirectResponse, secure: bool) -> None:
    for name in ("state", "nonce", "verifier", "return"):
        response.delete_cookie(
            _cookie_name(name, secure),
            path="/",
            secure=secure,
            httponly=True,
            samesite="lax",
        )


def _app_redirect(path: str, error: str | None = None) -> str:
    try:
        config = get_oidc_config()
        redirect = urlsplit(config.redirect_uri)
        query = f"auth_error={error}" if error else ""
        return urlunsplit((redirect.scheme, redirect.netloc, path, query, ""))
    except ValueError:
        return f"{path}?auth_error={error}" if error else path


def _oidc_error(secure: bool, code: str) -> RedirectResponse:
    response = RedirectResponse(_app_redirect("/", code), status_code=302)
    _clear_attempt_cookies(response, secure)
    response.headers["Cache-Control"] = "no-store"
    return response


def _record_login_attempt(db: Session, request: Request, success: bool) -> None:
    """Insert a row into login_attempts. Failures aren't fatal — never let
    audit-log writes break the auth flow itself."""
    try:
        db.add(
            LoginAttempt(
                ip=_client_ip(request),
                user_agent=(request.headers.get("user-agent") or "")[:255],
                success=success,
            )
        )
        db.commit()
    except Exception:
        db.rollback()


class PasswordPayload(StrictModel):
    password: str = Field(..., min_length=1, max_length=512)
    # Server Edition: required only when more than one user exists.
    username: Optional[str] = Field(None, max_length=100)


class SetupPayload(StrictModel):
    """First-run setup. Password is required; everything else is optional and
    falls back to the DEFAULT_SETTINGS values if blank."""

    password: str = Field(..., min_length=1, max_length=512)

    # Company
    company_name: Optional[str] = Field(None, max_length=200)
    company_address1: Optional[str] = Field(None, max_length=200)
    company_address2: Optional[str] = Field(None, max_length=200)
    company_city: Optional[str] = Field(None, max_length=100)
    company_state: Optional[str] = Field(None, max_length=50)
    company_zip: Optional[str] = Field(None, max_length=20)
    company_phone: Optional[str] = Field(None, max_length=50)
    company_email: Optional[str] = Field(None, max_length=200)
    company_website: Optional[str] = Field(None, max_length=200)
    company_tax_id: Optional[str] = Field(None, max_length=50)

    # Operator
    operator_name: Optional[str] = Field(None, max_length=200)
    operator_email: Optional[str] = Field(None, max_length=200)

    # Defaults applied to new invoices
    default_terms: Optional[str] = Field(None, max_length=100)
    default_tax_rate: Optional[str] = Field(None, max_length=20)


# Settings keys we accept on /setup (anything else on the payload is ignored)
_SETUP_SETTINGS_KEYS = (
    "company_name",
    "company_address1",
    "company_address2",
    "company_city",
    "company_state",
    "company_zip",
    "company_phone",
    "company_email",
    "company_website",
    "company_tax_id",
    "operator_name",
    "operator_email",
    "default_terms",
    "default_tax_rate",
)


@router.get("/status")
def auth_status(request: Request, db: Session = Depends(get_db)):
    """Tell the SPA whether first-run setup is needed and whether the
    current session is authenticated."""
    authenticated = request.session.get("authenticated") is True
    setup_needed = not password_is_set(db)
    oidc_enabled = is_oidc_enabled()
    out = {
        "setup_needed": setup_needed,
        "authenticated": authenticated,
        # Login UI shows a username field only when this is true.
        "multi_user": is_multi_user(db),
        "authentik_enabled": oidc_enabled,
        # Password auth remains a loopback-only break-glass path whenever
        # the public deployment has Authentik enabled.
        "local_password_login": not oidc_enabled or _is_loopback_request(request),
    }
    if setup_needed:
        # First-run setup can be reached on a file that already holds a
        # company's books (a file copied in, or seeded through the API
        # before anyone set a password). The form prefills the name the
        # books already carry and warns, so setup does not silently rename
        # another company's ledger (2.9.0 gate).
        from app.models.transactions import Transaction
        from app.services.settings_service import get_setting_raw

        out["company_name"] = get_setting_raw(db, "company_name") or ""
        out["has_data"] = db.query(Transaction.id).first() is not None
    if authenticated and request.session.get("username"):
        out["user"] = {
            "username": request.session.get("username"),
            "display_name": request.session.get("display_name") or "",
            "role": request.session.get("role") or "admin",
            "email": request.session.get("oidc_email") or "",
        }
        out["workspace"] = {
            "id": request.session.get("workspace_id") or "private",
            "label": request.session.get("workspace_label") or "Private books",
            "isolation": "dedicated-stack",
        }
    return out


@router.get("/authentik")
async def authentik_login(request: Request):
    """Start Authorization Code + PKCE against the governed Ark Authentik app."""
    secure = _is_secure_request(request)
    if not is_oidc_enabled():
        return _oidc_error(secure, "unavailable")
    try:
        config = get_oidc_config()
        discovery = await discover_oidc(config)
        attempt = create_oidc_attempt()
        response = RedirectResponse(
            build_authorization_url(discovery, config, attempt), status_code=302
        )
        for name, value in (
            ("state", attempt.state),
            ("nonce", attempt.nonce),
            ("verifier", attempt.verifier),
            ("return", _safe_next(request.query_params.get("next"))),
        ):
            _set_attempt_cookie(response, name, value, secure)
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception:
        logger.exception("Authentik OIDC authorization start failed")
        return _oidc_error(secure, "unavailable")


@router.get("/authentik/callback")
async def authentik_callback(request: Request, db: Session = Depends(get_db)):
    """Verify the Authentik callback, then issue an Ark CPA session."""
    secure = _is_secure_request(request)
    if request.query_params.get("error"):
        return _oidc_error(secure, "denied")
    if not is_oidc_enabled():
        return _oidc_error(secure, "unavailable")

    state = request.query_params.get("state") or ""
    code = request.query_params.get("code") or ""
    expected_state = request.cookies.get(_cookie_name("state", secure)) or ""
    nonce = request.cookies.get(_cookie_name("nonce", secure)) or ""
    verifier = request.cookies.get(_cookie_name("verifier", secure)) or ""
    return_to = _safe_next(request.cookies.get(_cookie_name("return", secure)))
    if (
        not code
        or not state
        or not expected_state
        or not nonce
        or not verifier
        or not secrets.compare_digest(state, expected_state)
    ):
        return _oidc_error(secure, "invalid_state")

    try:
        config = get_oidc_config()
        discovery = await discover_oidc(config)
        id_token = await exchange_authorization_code(code, verifier, discovery, config)
        claims = await verify_id_token(id_token, nonce, discovery, config)

        from app.services.oidc_identity import resolve_oidc_principal

        user = resolve_oidc_principal(db, claims, config.issuer)
        db.info["acting_username"] = user.username
        user.last_login_at = datetime.now(timezone.utc)
        db.commit()
        _record_login_attempt(db, request, success=True)

        request.session.clear()
        request.session["authenticated"] = True
        _stash_user(request, user)
        request.session["oidc_sub"] = str(claims["sub"])
        request.session["oidc_email"] = str(claims["email"]).strip().lower()
        request.session["workspace_id"] = (
            os.environ.get("ARKCPA_WORKSPACE_ID") or "private"
        ).strip()
        request.session["workspace_label"] = (
            os.environ.get("ARKCPA_WORKSPACE_LABEL") or "Private books"
        ).strip()

        response = RedirectResponse(_app_redirect(return_to), status_code=302)
        _clear_attempt_cookies(response, secure)
        response.headers["Cache-Control"] = "no-store"
        return response
    except PermissionError:
        _record_login_attempt(db, request, success=False)
        logger.warning("Authentik OIDC login rejected by identity policy")
        return _oidc_error(secure, "forbidden")
    except Exception:
        _record_login_attempt(db, request, success=False)
        logger.exception("Authentik OIDC callback failed")
        return _oidc_error(secure, "failed")


@router.post("/setup")
def setup(
    payload: SetupPayload,
    request: Request,
    db: Session = Depends(get_db),
):
    """First-run setup: store company/operator info and the operator password
    in one transaction, then issue a session. Returns 409 if a password is
    already set."""
    if is_oidc_enabled() and not _is_loopback_request(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Initial setup is only available from loopback",
        )
    if password_is_set(db):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Password already set — use /login",
        )

    if payload.company_name:
        from app.services.company_service import (
            _current_company_file,
            company_name_taken_by,
        )

        other = company_name_taken_by(
            payload.company_name, exclude_file=_current_company_file()
        )
        if other:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Another company file ({other}) is already named "
                    f"'{payload.company_name.strip()}'. Choose a name that "
                    "tells the two apart."
                ),
            )

    # Persist any non-blank settings the user provided. set_password() will
    # commit at the end, so all writes land in a single transaction.
    payload_dict = payload.model_dump()
    for key in _SETUP_SETTINGS_KEYS:
        value = payload_dict.get(key)
        if value is not None and value != "":
            set_setting(db, key, value)

    set_password(db, payload.password)
    # Materialize the operator as the admin user row right away (Server
    # Edition principal model) — same password, zero extra questions.
    admin = ensure_admin_user(db)
    if payload.company_name:
        from app.services.company_service import sync_manifest_name

        sync_manifest_name(payload.company_name)
    # Rotate session before issuing — clears anything an attacker might have
    # planted via a fixation attempt. Starlette's signed-cookie session is
    # already fixation-resistant (signature changes with payload) but this
    # is defence in depth and intent-revealing.
    request.session.clear()
    request.session["authenticated"] = True
    _stash_user(request, admin)
    return {"status": "ok", "authenticated": True}


def _stash_user(request: Request, user) -> None:
    """Record the acting principal in the session (None = legacy no-row)."""
    if user is None:
        return
    request.session["user_id"] = user.id
    request.session["username"] = user.username
    request.session["display_name"] = user.display_name
    request.session["role"] = user.role


@router.post("/login")
@limiter.limit("5/minute")
def login(
    request: Request,
    payload: PasswordPayload,
    db: Session = Depends(get_db),
):
    """Verify the operator password and issue a session.

    Rate-limited to 5/minute per IP to kill fast brute-force. argon2id's
    ~100ms-per-verify cost is the second line. Every attempt — success or
    failure — is recorded in `login_attempts` so a slow patient attacker
    pacing requests under the rate limit still shows up in the audit log.
    """
    if is_oidc_enabled() and not _is_loopback_request(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Use Authentik to sign in",
        )
    if not password_is_set(db):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Setup required — set a password first",
        )
    if is_multi_user(db) and not (payload.username or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username required",
        )
    user = authenticate(db, payload.password, username=payload.username)
    if user is None:
        _record_login_attempt(db, request, success=False)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Incorrect username or password"
                if is_multi_user(db)
                else "Incorrect password"
            ),
        )
    _record_login_attempt(db, request, success=True)
    # Same rotation rationale as /setup.
    request.session.clear()
    request.session["authenticated"] = True
    _stash_user(request, user)
    return {"status": "ok", "authenticated": True}


@router.post("/logout")
def logout(request: Request):
    """Clear the session cookie."""
    request.session.clear()
    return {"status": "ok"}
