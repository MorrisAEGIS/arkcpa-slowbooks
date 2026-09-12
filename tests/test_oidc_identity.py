"""Immutable Authentik-to-ArkCPA principal binding tests."""

import pytest

from app.models.users import ROLE_ADMIN, ROLE_READONLY, User
from app.services.oidc_identity import resolve_oidc_principal

ISSUER = "https://auth.example/application/o/arkcpa/"


def _claims(subject="subject-jay", email="jay@example.com", username="jay"):
    return {
        "sub": subject,
        "email": email,
        "preferred_username": username,
        "name": "Jay Morris",
    }


def _env(email="jay@example.com"):
    return {"AUTHENTIK_OIDC_BOOTSTRAP_EMAIL": email}


def test_empty_private_workspace_creates_oidc_only_admin(db_session):
    user = resolve_oidc_principal(db_session, _claims(), ISSUER, _env())
    db_session.commit()

    assert user.username == "jay"
    assert user.role == ROLE_ADMIN
    assert user.password_hash == "!oidc-only"
    assert user.oidc_issuer == ISSUER
    assert user.oidc_subject == "subject-jay"
    assert user.email == "jay@example.com"


def test_existing_single_admin_is_upgraded_in_place(db_session):
    legacy = User(
        username="admin",
        display_name="Operator",
        password_hash="legacy-hash",
        role=ROLE_ADMIN,
        is_active=True,
    )
    db_session.add(legacy)
    db_session.commit()
    legacy_id = legacy.id

    bound = resolve_oidc_principal(db_session, _claims(), ISSUER, _env())
    db_session.commit()

    assert bound.id == legacy_id
    assert bound.username == "jay"
    assert bound.password_hash == "legacy-hash"
    assert db_session.query(User).count() == 1


def test_bound_subject_not_email_controls_later_login(db_session):
    user = resolve_oidc_principal(db_session, _claims(), ISSUER, _env())
    db_session.commit()

    resolved = resolve_oidc_principal(
        db_session,
        _claims(email="jay.new@example.com"),
        ISSUER,
        {},
    )
    db_session.commit()

    assert resolved.id == user.id
    assert resolved.email == "jay.new@example.com"


def test_bound_subject_cannot_claim_another_principals_email(db_session):
    user = resolve_oidc_principal(db_session, _claims(), ISSUER, _env())
    db_session.add(
        User(
            username="maesa",
            display_name="Maesa",
            email="maesa@example.com",
            password_hash="local-recovery-hash",
            role="bookkeeper",
            is_active=True,
        )
    )
    db_session.commit()

    with pytest.raises(PermissionError, match="assigned to another principal"):
        resolve_oidc_principal(
            db_session,
            _claims(subject=user.oidc_subject, email="maesa@example.com"),
            ISSUER,
            {},
        )


def test_unprovisioned_email_is_rejected(db_session):
    with pytest.raises(PermissionError, match="not provisioned"):
        resolve_oidc_principal(
            db_session,
            _claims(email="maesa@example.com", username="maesa"),
            ISSUER,
            _env("jay@example.com"),
        )
    assert db_session.query(User).count() == 0


def test_explicit_unbound_user_email_can_bind_second_authentik_principal(db_session):
    user = User(
        username="maesa",
        display_name="Maesa",
        email="maesa@example.com",
        password_hash="local-recovery-hash",
        role="bookkeeper",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    resolved = resolve_oidc_principal(
        db_session,
        _claims(subject="subject-maesa", email="maesa@example.com", username="maesa"),
        ISSUER,
        _env("jay@example.com"),
    )
    db_session.commit()

    assert resolved.id == user.id
    assert resolved.role == "bookkeeper"
    assert resolved.oidc_subject == "subject-maesa"


def test_subject_change_for_same_email_is_rejected(db_session):
    resolve_oidc_principal(db_session, _claims(), ISSUER, _env())
    db_session.commit()

    with pytest.raises(PermissionError, match="subject changed"):
        resolve_oidc_principal(
            db_session,
            _claims(subject="attacker-subject"),
            ISSUER,
            _env(),
        )


def test_ambiguous_existing_users_fail_closed(db_session):
    db_session.add_all(
        [
            User(
                username="admin",
                display_name="Admin",
                password_hash="hash",
                role=ROLE_ADMIN,
                is_active=True,
            ),
            User(
                username="viewer",
                display_name="Viewer",
                password_hash="hash",
                role=ROLE_READONLY,
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    with pytest.raises(PermissionError, match="provisioned explicitly"):
        resolve_oidc_principal(db_session, _claims(), ISSUER, _env())
