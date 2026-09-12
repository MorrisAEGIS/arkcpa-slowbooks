"""Server Edition RBAC (PR-S3): role policy enforcement, /api/users
management, last-admin protection, and per-user audit attribution."""

from app.models.audit import AuditLog
from app.models.users import ROLE_BOOKKEEPER, ROLE_READONLY, User
from app.services import auth as auth_service

FIXTURE_PW = "test-password-123"


def _mk_user(db, username, password, role):
    u = User(
        username=username,
        display_name=username.title(),
        password_hash=auth_service.hash_password(password),
        role=role,
        is_active=True,
    )
    db.add(u)
    db.commit()
    return u


def _login_as(client, username, password):
    client.post("/api/auth/logout")
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, r.text
    return r


# ---------------------------------------------------------------------------
# Role policy
# ---------------------------------------------------------------------------


def test_readonly_can_read_but_not_write(client, db_session):
    _mk_user(db_session, "viewer", "viewer-password-1", ROLE_READONLY)
    _login_as(client, "viewer", "viewer-password-1")

    assert client.get("/api/customers").status_code == 200
    assert client.get("/api/reports/profit-loss").status_code == 200
    r = client.post("/api/customers", json={"name": "Blocked Corp"})
    assert r.status_code == 403


def test_bookkeeper_daily_writes_ok_admin_writes_blocked(client, db_session):
    _mk_user(db_session, "keeper", "keeper-password-1", ROLE_BOOKKEEPER)
    _login_as(client, "keeper", "keeper-password-1")

    r = client.post("/api/customers", json={"name": "Daily Books LLC"})
    assert r.status_code in (200, 201)

    assert client.put("/api/settings", json={"invoice_prefix": "X-"}).status_code == 403
    assert client.get("/api/settings").status_code == 200  # reads stay open
    assert (
        client.post(
            "/api/users",
            json={
                "username": "sneaky",
                "password": "sneaky-password-1",
                "role": "admin",
            },
        ).status_code
        == 403
    )
    # Reads of the user list are role-gated in-route too
    assert client.get("/api/users").status_code == 403


def test_admin_unrestricted_and_legacy_session_is_admin(client):
    # The fixture session predates any explicit role handling in older
    # cookies; ours carries role=admin from setup — both must pass.
    assert client.put("/api/settings", json={"invoice_prefix": "A-"}).status_code == 200
    assert client.get("/api/users").status_code == 200


# ---------------------------------------------------------------------------
# /api/users management
# ---------------------------------------------------------------------------


def test_create_list_update_user(client):
    r = client.post(
        "/api/users",
        json={
            "username": "Renita",
            "display_name": "Renita",
            "email": "RENITA@EXAMPLE.COM",
            "password": "renita-password-1",
            "role": "bookkeeper",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["username"] == "renita"  # normalized lowercase
    assert body["email"] == "renita@example.com"
    assert "password" not in str(body) or "password_hash" not in body

    users = client.get("/api/users").json()
    assert [u["username"] for u in users] == ["admin", "renita"]

    r = client.put(f"/api/users/{body['id']}", json={"role": "readonly"})
    assert r.status_code == 200
    assert r.json()["role"] == "readonly"


def test_authentik_email_must_be_valid_and_unique(client):
    first = client.post(
        "/api/users",
        json={
            "username": "first-user",
            "email": "first@example.com",
            "password": "first-user-password",
            "role": "bookkeeper",
        },
    )
    assert first.status_code == 201

    invalid = client.post(
        "/api/users",
        json={
            "username": "bad-email",
            "email": "not-an-email",
            "password": "bad-email-password",
            "role": "readonly",
        },
    )
    assert invalid.status_code == 400

    duplicate = client.post(
        "/api/users",
        json={
            "username": "second-user",
            "email": "FIRST@EXAMPLE.COM",
            "password": "second-user-password",
            "role": "readonly",
        },
    )
    assert duplicate.status_code == 409


def test_user_validation(client):
    bad = [
        ({"username": "x", "password": "long-enough-pw", "role": "admin"}, 422),
        ({"username": "has space", "password": "long-enough-pw", "role": "admin"}, 400),
        ({"username": "goodname", "password": "short", "role": "admin"}, 400),
        ({"username": "goodname", "password": "long-enough-pw", "role": "boss"}, 400),
    ]
    for payload, expected in bad:
        r = client.post("/api/users", json=payload)
        assert r.status_code == expected, (payload, r.status_code)

    client.post(
        "/api/users",
        json={"username": "dupe", "password": "long-enough-pw", "role": "readonly"},
    )
    r = client.post(
        "/api/users",
        json={"username": "DUPE", "password": "long-enough-pw", "role": "readonly"},
    )
    assert r.status_code == 409


def test_last_admin_protected(client, db_session):
    admin_id = db_session.query(User).filter(User.username == "admin").one().id
    assert (
        client.put(f"/api/users/{admin_id}", json={"role": "readonly"}).status_code
        == 409
    )
    assert (
        client.put(f"/api/users/{admin_id}", json={"is_active": False}).status_code
        == 409
    )
    # With a second admin present, demotion is allowed
    client.post(
        "/api/users",
        json={"username": "admin2", "password": "admin2-password-1", "role": "admin"},
    )
    assert (
        client.put(f"/api/users/{admin_id}", json={"role": "bookkeeper"}).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# Audit attribution
# ---------------------------------------------------------------------------


def test_audit_rows_attributed_to_acting_user(client, db_session):
    _mk_user(db_session, "keeper", "keeper-password-1", ROLE_BOOKKEEPER)
    _login_as(client, "keeper", "keeper-password-1")
    r = client.post("/api/customers", json={"name": "Attributed Inc"})
    assert r.status_code in (200, 201)

    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.table_name == "customers")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    assert row.username == "keeper"


def test_audit_never_stores_password_hashes(client, db_session):
    client.post(
        "/api/users",
        json={
            "username": "hashcheck",
            "password": "hash-password-11",
            "role": "readonly",
        },
    )
    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.table_name == "users")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    assert row.username == "admin"
    assert row.new_values.get("password_hash") == "***"
    assert "argon2" not in str(row.new_values)


def test_audit_api_returns_username_field(client, db_session):
    """Regression: the /api/audit response model must expose `username`.

    The write path was verified against the DB while the response model
    silently stripped the field — every layer looked healthy except the
    one users see. Read THROUGH THE API, like the UI does."""
    _mk_user(db_session, "keeper", "keeper-password-1", ROLE_BOOKKEEPER)
    _login_as(client, "keeper", "keeper-password-1")
    client.post("/api/customers", json={"name": "API Visibility Inc"})

    rows = client.get("/api/audit", params={"table_name": "customers"}).json()
    assert rows, "expected at least one customers audit row"
    assert "username" in rows[0], "response model is stripping username"
    assert rows[0]["username"] == "keeper"


def test_login_audit_row_is_self_attributed(client, db_session):
    """Field finding: every login wrote the most visible unattributed row
    (last_login_at UPDATE flushes before the session carries a username)."""
    _mk_user(db_session, "keeper", "keeper-password-1", ROLE_BOOKKEEPER)
    _login_as(client, "keeper", "keeper-password-1")
    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.table_name == "users", AuditLog.action == "UPDATE")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    assert row.username == "keeper"


def test_readonly_cannot_read_audit_payloads(client, db_session):
    """Field finding: audit snapshots carry full historical PII — readonly
    means read the books, not read every value any field ever held."""
    _mk_user(db_session, "viewer", "viewer-password-1", ROLE_READONLY)
    _login_as(client, "viewer", "viewer-password-1")
    assert client.get("/api/audit").status_code == 403
    assert client.get("/api/audit/tables").status_code == 403
    assert client.get("/api/customers").status_code == 200  # books stay readable
