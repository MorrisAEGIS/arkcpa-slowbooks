"""Fail-closed binding between Authentik identities and ArkCPA principals.

ArkCPA private workspaces are deployed as separate stacks. Each stack has
its own Authentik application/group, database, file volume, backup volume,
and encryption secrets. Inside a stack this module additionally pins the
approved operator to Authentik's immutable ``(issuer, sub)`` identity so a
later email or display-name change cannot transfer the account.
"""

from __future__ import annotations

import os
import re
import secrets
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.models.users import ROLE_ADMIN, User

_USERNAME_SAFE = re.compile(r"[^a-z0-9._-]+")
_OIDC_ONLY_PASSWORD = "!oidc-only"


def _normalized_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _username_from_claims(claims: Mapping[str, Any], email: str) -> str:
    raw = str(claims.get("preferred_username") or email.split("@", 1)[0] or "owner")
    username = _USERNAME_SAFE.sub("-", raw.strip().lower()).strip(".-_")
    return (username or "owner")[:50]


def _unique_username(db: Session, preferred: str) -> str:
    if db.query(User.id).filter(User.username == preferred).first() is None:
        return preferred
    for suffix in range(2, 1000):
        candidate = f"{preferred[: 50 - len(str(suffix)) - 1]}-{suffix}"
        if db.query(User.id).filter(User.username == candidate).first() is None:
            return candidate
    raise PermissionError("No safe local username is available")


def _display_name(claims: Mapping[str, Any], username: str) -> str:
    name = str(
        claims.get("name")
        or claims.get("preferred_username")
        or username.replace(".", " ").replace("-", " ").title()
    ).strip()
    return (name or username)[:200]


def resolve_oidc_principal(
    db: Session,
    claims: Mapping[str, Any],
    issuer: str,
    env: Mapping[str, str] | os._Environ[str] = os.environ,
) -> User:
    """Return the exact local principal for verified OIDC claims.

    Existing bindings resolve only by ``(issuer, sub)``. The first binding
    is allowed only when the verified email exactly matches the stack's
    explicit ``AUTHENTIK_OIDC_BOOTSTRAP_EMAIL``. That one-time value is not
    used for later logins and cannot become account recovery or transfer.
    """

    subject = str(claims.get("sub") or "").strip()
    email = _normalized_email(claims.get("email"))
    normalized_issuer = str(issuer or "").strip()
    if not subject or not email or not normalized_issuer:
        raise PermissionError("OIDC identity is missing required claims")

    user = (
        db.query(User)
        .filter(
            User.oidc_issuer == normalized_issuer,
            User.oidc_subject == subject,
        )
        .first()
    )
    if user is not None:
        if not user.is_active:
            raise PermissionError("OIDC principal is inactive")
        # Profile drift is safe to refresh after immutable identity match.
        user.email = email
        user.display_name = _display_name(claims, user.username)
        return user

    bootstrap_email = _normalized_email(env.get("AUTHENTIK_OIDC_BOOTSTRAP_EMAIL"))
    if not bootstrap_email or not secrets.compare_digest(email, bootstrap_email):
        raise PermissionError("OIDC identity is not provisioned for this workspace")

    # Refuse an attempted subject change for an already-bound email. An
    # operator must clear/rebind it through an explicit recovery procedure.
    if (
        db.query(User.id)
        .filter(User.email == email, User.oidc_subject.is_not(None))
        .first()
        is not None
    ):
        raise PermissionError("OIDC subject changed for a provisioned identity")

    all_users = db.query(User).order_by(User.id).all()
    unbound_admins = [
        candidate
        for candidate in all_users
        if candidate.is_active
        and candidate.role == ROLE_ADMIN
        and not candidate.oidc_subject
        and not candidate.oidc_issuer
    ]
    preferred_username = _username_from_claims(claims, email)

    if not all_users:
        user = User(
            username=_unique_username(db, preferred_username),
            display_name=_display_name(claims, preferred_username),
            password_hash=_OIDC_ONLY_PASSWORD,
            role=ROLE_ADMIN,
            is_active=True,
        )
        db.add(user)
    elif len(all_users) == 1 and len(unbound_admins) == 1:
        # Upgrade the historical single local admin in place, retaining its
        # role and audit continuity while giving the human a real identity.
        user = unbound_admins[0]
        if user.username == "admin":
            user.username = _unique_username(db, preferred_username)
        user.display_name = _display_name(claims, user.username)
    else:
        raise PermissionError("Workspace principal must be provisioned explicitly")

    user.oidc_issuer = normalized_issuer
    user.oidc_subject = subject
    user.email = email
    db.flush()
    return user
