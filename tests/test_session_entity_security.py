"""Adversarial authority tests against disposable databases only."""

import base64
import json
from datetime import date

import pytest
from itsdangerous import TimestampSigner
from fastapi import HTTPException
from starlette.requests import Request

from app import database as database_module
from app.database import get_db
from app.main import app
from app.models.users import User
from app.models.arkcpa import (
    ArkEntity,
    ArkEntityAccess,
    ArkEvidence,
    ArkPostingCandidate,
)
from app.models.contacts import Customer


def _cookie(session):
    return (
        TimestampSigner("test-secret-key-not-for-production")
        .sign(base64.b64encode(json.dumps(session).encode()))
        .decode()
    )


def test_oidc_password_fallback_denies_spoofed_headers_even_with_incomplete_config(
    unauthed_client, monkeypatch
):
    monkeypatch.setenv("AUTHENTIK_OIDC_ENABLED", "true")
    monkeypatch.delenv("AUTHENTIK_OIDC_CLIENT_SECRET", raising=False)
    for headers in (
        {},
        {"host": "localhost"},
        {"x-forwarded-host": "localhost"},
        {"x-forwarded-host": "127.0.0.1", "x-forwarded-for": "127.0.0.1"},
        {"forwarded": "for=127.0.0.1;host=localhost"},
    ):
        status = unauthed_client.get("/api/auth/status", headers=headers)
        assert status.json()["local_password_login"] is False
        for path in ("/api/auth/login", "/api/auth/setup"):
            assert (
                unauthed_client.post(
                    path, headers=headers, json={"password": "synthetic-long-password"}
                ).status_code
                == 403
            )


@pytest.mark.parametrize(
    "change", ["inactive", "version", "missing_version", "missing_role", "invented_id"]
)
def test_stale_or_incomplete_signed_sessions_are_denied_everywhere(
    client, db_session, change
):
    user = db_session.query(User).filter_by(username="admin").one()
    session = {
        "authenticated": True,
        "user_id": user.id,
        "username": user.username,
        "role": "admin",
        "session_version": user.session_version,
    }
    if change == "inactive":
        user.is_active = False
    elif change == "version":
        user.session_version += 1
    elif change == "missing_version":
        del session["session_version"]
    elif change == "missing_role":
        del session["role"]
    elif change == "invented_id":
        session["user_id"] = user.id + 12345
    db_session.commit()
    for path in (
        "/api/users",
        "/api/tokens",
        "/api/customers",
        "/api/ark-cpa/entities",
    ):
        client.cookies.clear()
        client.cookies.set("slowbooks_session", _cookie(session))
        assert client.get(path).status_code == 401
    client.cookies.clear()
    client.cookies.set("slowbooks_session", _cookie(session))
    assert client.get("/api/auth/status").json()["authenticated"] is False


def test_database_role_wins_over_old_signed_admin_role(client, db_session):
    user = db_session.query(User).filter_by(username="admin").one()
    user.role = "readonly"
    db_session.commit()
    assert client.get("/api/auth/status").json()["user"]["role"] == "readonly"
    assert client.get("/api/users").status_code == 403
    assert client.post("/api/customers", json={"name": "Denied"}).status_code == 403
    assert client.get("/api/customers").status_code == 200


def test_logout_revokes_replayed_cookie(client):
    old_cookie = client.cookies.get("slowbooks_session")
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.clear()
    client.cookies.set("slowbooks_session", old_cookie)
    assert client.get("/api/users").status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"role": "readonly"},
        {"is_active": False},
        {"password": "changed-synthetic-password"},
    ],
)
def test_admin_user_changes_revoke_existing_sessions(client, db_session, payload):
    user = db_session.query(User).filter_by(username="admin").one()
    old_cookie = client.cookies.get("slowbooks_session")
    assert (
        client.post(
            "/api/users",
            json={
                "username": "backup-admin",
                "role": "admin",
                "password": "backup-synthetic-password",
            },
        ).status_code
        == 201
    )
    assert client.put(f"/api/users/{user.id}", json=payload).status_code == 200
    client.cookies.clear()
    client.cookies.set("slowbooks_session", old_cookie)
    assert client.get("/api/users").status_code == 401


def _entity_fixture(
    client,
    db_session,
    TestSession,
    monkeypatch,
    role="admin",
    mode="assisted",
    facts="verified",
):
    user = db_session.query(User).filter_by(username="admin").one()
    entity = ArkEntity(
        name="Synthetic ledger",
        slug="synthetic-ledger",
        entity_type="us_tx_llc",
        jurisdiction="US/TX",
        database_name="synthetic_ledger",
        ark_files_path="/tmp/synthetic-no-documents",
        status="active",
        posting_mode=mode,
        facts_status=facts,
    )
    db_session.add(entity)
    db_session.flush()
    db_session.add(
        ArkEntityAccess(
            entity_id=entity.id,
            user_id=user.id,
            role=role,
            is_default=True,
            protected_approver=True,
        )
    )
    db_session.commit()
    monkeypatch.setenv("ARKCPA_ENTITY_MODE", "true")
    monkeypatch.setattr(
        database_module, "entity_session_factory", lambda name: TestSession
    )
    monkeypatch.delitem(app.dependency_overrides, get_db)
    return user, entity


@pytest.mark.parametrize(
    "role,mode,facts,status",
    [
        ("readonly", "assisted", "verified", 403),
        ("admin", "frozen", "verified", 409),
        ("bookkeeper", "assisted", "hold", 409),
    ],
)
def test_legacy_ledger_writes_use_entity_permissions(
    client, db_session, TestSession, monkeypatch, role, mode, facts, status
):
    _entity_fixture(client, db_session, TestSession, monkeypatch, role, mode, facts)
    assert client.get("/api/customers").status_code == 200
    assert (
        client.post(
            "/api/customers", json={"name": "Blocked synthetic customer"}
        ).status_code
        == status
    )
    assert (
        client.post(
            "/api/journal",
            json={"date": "2026-10-11", "description": "Blocked", "lines": []},
        ).status_code
        == status
    )
    assert db_session.query(Customer).count() == 0
    # Control metadata remains reachable while ledger writes are denied.
    assert client.get("/api/ark-cpa/entities").status_code == 200


def test_authorized_unfrozen_entity_write_still_works(
    client, db_session, TestSession, monkeypatch
):
    _entity_fixture(client, db_session, TestSession, monkeypatch)
    assert (
        client.post(
            "/api/customers", json={"name": "Allowed synthetic customer"}
        ).status_code
        == 201
    )
    assert db_session.query(Customer).count() == 1


def test_unknown_entity_and_missing_identity_fail_closed(
    client, db_session, TestSession, monkeypatch
):
    _entity_fixture(client, db_session, TestSession, monkeypatch)
    assert (
        client.get(
            "/api/customers", headers={"X-Ark-Entity": "another-company"}
        ).status_code
        == 403
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/customers",
            "headers": [],
            "session": {},
        }
    )
    with pytest.raises(HTTPException) as caught:
        database_module._entity_database_for_request(request)
    assert caught.value.status_code == 401


def test_explicit_review_dependency_is_read_only_even_on_frozen_entity(
    client, db_session, TestSession, monkeypatch
):
    user, entity = _entity_fixture(
        client, db_session, TestSession, monkeypatch, mode="frozen", facts="hold"
    )
    for path in (
        "/api/ark-cpa/candidates/1/evaluate",
        "/api/ark-cpa/candidates/1/controller-run",
    ):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [],
                "query_string": b"",
                "session": {"authenticated": True, "username": user.username},
            }
        )
        dependency = get_db(request)
        ledger = next(dependency)
        ledger.add(Customer(name="Forbidden from review"))
        with pytest.raises(HTTPException) as caught:
            ledger.commit()
        assert caught.value.status_code == 403
        dependency.close()
    assert db_session.query(Customer).count() == 0


@pytest.mark.parametrize(
    "origin",
    [
        "https://sibling.enerteqpower.com",
        "http://testserver.evil.invalid",
        "null",
        "https://evil.invalid",
    ],
)
def test_browser_form_from_untrusted_origin_cannot_logout(client, origin):
    response = client.post("/api/auth/logout", headers={"Origin": origin}, data={})
    assert response.status_code == 403
    assert response.json()["detail"] == "Untrusted browser origin"
    assert client.get("/api/auth/status").json()["authenticated"] is True


def test_same_origin_browser_write_works_and_missing_browser_origin_fails(client):
    assert (
        client.post(
            "/api/customers",
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
            json={"name": "Trusted"},
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/auth/logout", headers={"Sec-Fetch-Site": "same-site"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/auth/logout", headers={"Origin": "http://testserver"}
        ).status_code
        == 200
    )


def test_canonical_oidc_origin_allowed_but_not_forwarded_host(client, monkeypatch):
    monkeypatch.setenv(
        "AUTHENTIK_OIDC_REDIRECT_URI",
        "https://cpa.enerteqpower.com/api/auth/authentik/callback",
    )
    assert (
        client.post(
            "/api/customers",
            headers={"Origin": "https://cpa.enerteqpower.com"},
            json={"name": "Canonical"},
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/auth/logout",
            headers={
                "Origin": "https://evil.invalid",
                "X-Forwarded-Host": "evil.invalid",
                "Host": "evil.invalid",
            },
        ).status_code
        == 403
    )


def test_frozen_workspace_settings_and_reports_remain_readable(
    client, db_session, TestSession, monkeypatch, seed_accounts
):
    _entity_fixture(
        client, db_session, TestSession, monkeypatch, mode="frozen", facts="hold"
    )
    for path in (
        "/api/ark-cpa/status",
        "/api/settings",
        "/api/dashboard/data",
        "/api/analytics/dashboard",
        "/api/reports/profit-loss",
        "/api/reports/balance-sheet",
        "/api/reports/trial-balance",
    ):
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code, response.text)


def test_public_payment_and_portal_fail_closed_in_entity_mode(
    client, db_session, TestSession, monkeypatch
):
    _entity_fixture(client, db_session, TestSession, monkeypatch)
    client.cookies.clear()
    for path in (
        "/pay/synthetic-unknown",
        "/portal/synthetic-unknown",
        "/portal/profile",
    ):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 401, (path, response.status_code)


def test_explicit_query_entity_must_match_selected_ledger(
    client, db_session, TestSession, monkeypatch
):
    user, entity = _entity_fixture(client, db_session, TestSession, monkeypatch)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/ark-cpa/review",
            "headers": [],
            "query_string": f"entity_id={entity.id + 1}".encode(),
            "session": {"authenticated": True, "username": user.username},
        }
    )
    with pytest.raises(HTTPException) as caught:
        database_module._entity_database_for_request(request)
    assert caught.value.status_code == 409


def test_entity_bookkeeper_limits_global_admin_on_legacy_routes(
    client, db_session, TestSession, monkeypatch
):
    _entity_fixture(client, db_session, TestSession, monkeypatch, role="bookkeeper")
    from app.models.payroll import Employee

    employee = Employee(
        first_name="Synthetic", last_name="Private", pay_rate=987, ssn_last_four="1234"
    )
    db_session.add(employee)
    db_session.commit()
    visible = client.get("/api/employees").json()[0]
    assert float(visible["pay_rate"]) == 0
    assert visible["ssn_last_four"] is None
    assert client.get("/api/employees/1/bank-accounts").status_code == 403
    assert (
        client.put("/api/settings", json={"company_name": "Forbidden"}).status_code
        == 403
    )
    assert client.post("/api/employees", json={}).status_code == 403
    assert (
        client.post(
            "/api/customers", json={"name": "Allowed by bookkeeper"}
        ).status_code
        == 201
    )
    assert client.get("/api/users").status_code == 200


def _candidate(db_session, entity):
    evidence = ArkEvidence(
        entity_id=entity.id,
        source_path="synthetic-no-documents",
        category="Receipts",
        content_hash="a" * 64,
        status="verified",
    )
    db_session.add(evidence)
    db_session.flush()
    candidate = ArkPostingCandidate(
        entity_id=entity.id,
        evidence_id=evidence.id,
        transaction_date=date(2026, 10, 11),
        description="Synthetic review",
        lines=[],
        created_by="admin",
    )
    db_session.add(candidate)
    db_session.commit()
    return candidate


def test_candidate_review_must_use_its_own_entity_ledger(
    client, db_session, TestSession, monkeypatch
):
    user, _entity = _entity_fixture(client, db_session, TestSession, monkeypatch)
    other = ArkEntity(
        name="Other synthetic ledger",
        slug="other-ledger",
        entity_type="us_tx_llc",
        jurisdiction="US/TX",
        database_name="other_ledger",
        ark_files_path="/tmp/synthetic-other-no-documents",
        status="active",
        posting_mode="assisted",
        facts_status="verified",
    )
    db_session.add(other)
    db_session.flush()
    db_session.add(ArkEntityAccess(entity_id=other.id, user_id=user.id, role="admin"))
    candidate = _candidate(db_session, other)
    for action in ("evaluate", "controller-run", "post"):
        response = client.post(
            f"/api/ark-cpa/candidates/{candidate.id}/{action}",
            headers={"X-Ark-Entity": "synthetic-ledger"},
        )
        assert response.status_code == 409
        assert "candidate's entity" in response.json()["detail"]
    db_session.refresh(candidate)
    assert candidate.status == "draft"
    assert candidate.deterministic_checks == {}


def test_frozen_entity_evaluation_updates_only_control_review(
    client, db_session, TestSession, monkeypatch
):
    _user, entity = _entity_fixture(
        client, db_session, TestSession, monkeypatch, mode="frozen", facts="hold"
    )
    candidate = _candidate(db_session, entity)
    response = client.post(f"/api/ark-cpa/candidates/{candidate.id}/evaluate")
    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert (
        "posting_mode_allows_entry" in response.json()["deterministic_checks"]["failed"]
    )
    assert db_session.query(Customer).count() == 0


def test_bearer_client_and_independent_webhook_bypass_only_browser_origin_gate(client):
    token = client.post(
        "/api/tokens", json={"label": "synthetic-origin-api", "role": "bookkeeper"}
    ).json()["token"]
    client.cookies.clear()
    response = client.post(
        "/api/customers",
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "https://outside.invalid",
        },
        json={"name": "Bearer authority"},
    )
    assert response.status_code == 201
    response = client.post(
        "/api/payments/stripe/webhook",
        headers={"Origin": "https://outside.invalid"},
        content=b"{}",
    )
    assert response.status_code == 400
    assert "origin" not in response.json()["detail"].lower()
