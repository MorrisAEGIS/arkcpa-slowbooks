"""Production API boundary: entity administration never grants global research."""

from datetime import timedelta

from app.models.arkcpa import (
    ArkAuthoritySource,
    ArkAuthorityRetrieval,
    ArkEntity,
    ArkEntityAccess,
)
from app.models.users import User
from app.services import arkcpa_tax as tax
from tests.test_arkcpa_tax import grant, research_source, capture, PASSAGE
from tests.test_arkcpa_tax import public_dns  # noqa: F401 - pytest fixture


def membership(db):
    user = db.query(User).filter_by(username="admin").one()
    entity = ArkEntity(
        name="Synthetic",
        slug="synthetic",
        entity_type="us_tx_llc",
        jurisdiction="United States / Texas",
        database_name="synthetic",
        ark_files_path="/synthetic",
        currency="USD",
        posting_mode="frozen",
        facts_status="hold",
    )
    db.add(entity)
    db.flush()
    db.add(
        ArkEntityAccess(
            entity_id=entity.id,
            user_id=user.id,
            role="admin",
            protected_approver=True,
            is_default=True,
        )
    )
    db.commit()


def test_get_has_no_seeding_and_entity_approver_cannot_refresh(
    client, db_session, monkeypatch
):
    membership(db_session)
    monkeypatch.setattr(
        tax,
        "fetch_source",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Network denied")),
    )
    response = client.get("/api/ark-cpa/authority-sources")
    assert response.status_code == 200
    assert response.json()["sources"] == [] and response.json()["can_refresh"] is False
    assert db_session.query(ArkAuthoritySource).count() == 0
    assert (
        client.post("/api/ark-cpa/authority-sources/refresh", json={}).status_code
        == 403
    )
    assert db_session.query(ArkAuthorityRetrieval).count() == 0
    assert (
        client.post(
            "/api/ark-cpa/authority-sources/permissions", json={"permission": "refresh"}
        ).status_code
        == 404
    )


def test_explicit_global_refresh_grant_rechecked_per_request(client, db_session):
    membership(db_session)
    row = grant(db_session, "admin", "refresh")
    assert client.get("/api/ark-cpa/authority-sources").json()["can_refresh"] is True
    assert (
        client.post("/api/ark-cpa/authority-sources/refresh", json={}).json()["results"]
        == []
    )
    row.enabled = False
    db_session.commit()
    assert (
        client.post("/api/ark-cpa/authority-sources/refresh", json={}).status_code
        == 403
    )
    assert client.get("/api/ark-cpa/authority-sources").json()["can_refresh"] is False


def test_snapshot_review_requires_own_permission_and_never_promotes(client, db_session):
    membership(db_session)
    source = research_source(db_session)
    snapshot = capture(db_session, source)
    url = f"/api/ark-cpa/authority-sources/snapshots/{snapshot.id}"
    assert client.get(url).status_code == 403
    assert (
        client.post(
            url + "/reviews",
            json={"decision": "rejected", "rationale": "Review denied"},
        ).status_code
        == 403
    )
    grant(db_session, "admin", "review")
    detail = client.get(url)
    assert detail.status_code == 200 and PASSAGE in detail.json()["extracted_text"]
    response = client.post(
        url + "/reviews",
        json={
            "decision": "approved",
            "rationale": "Synthetic review only",
            "applicability": {
                "jurisdictions": ["United States / Texas"],
                "entity_types": ["us_tx_llc"],
                "topics": ["bookkeeping"],
            },
            "effective_from": "2026-01-01",
            "valid_until": (tax._now() + timedelta(days=1)).isoformat(),
            "passages": [{"text": PASSAGE, "locator": "p1"}],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["automatic_policy_change"] is False
    bad = client.post(
        url + "/reviews",
        json={"decision": "approved", "rationale": "Invented", "applicability": {}},
    )
    assert bad.status_code == 422


def test_agent_token_cannot_borrow_human_research_grant(client, db_session):
    membership(db_session)
    grant(db_session, "admin", "refresh")
    response = client.post(
        "/api/tokens", json={"label": "synthetic-research-agent", "role": "admin"}
    )
    assert response.status_code in {200, 201}, response.text
    token = response.json()["token"]
    response = client.post(
        "/api/ark-cpa/authority-sources/refresh",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert response.status_code == 403
    assert db_session.query(ArkAuthorityRetrieval).count() == 0


def test_token_without_session_cannot_refresh(client, db_session):
    membership(db_session)
    grant(db_session, "admin", "refresh")
    token = client.post(
        "/api/tokens", json={"label": "synthetic-token-only", "role": "admin"}
    ).json()["token"]
    client.cookies.clear()
    response = client.post(
        "/api/ark-cpa/authority-sources/refresh",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert response.status_code == 403
    assert db_session.query(ArkAuthorityRetrieval).count() == 0
