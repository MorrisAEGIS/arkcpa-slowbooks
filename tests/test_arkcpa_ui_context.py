"""Tab context must select the ledger explicitly and retain server authority."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database as database
from app.main import app
from app.models.arkcpa import ArkEntity, ArkEntityAccess
from app.models.contacts import Customer
from tests.test_session_entity_security import _entity_fixture


@pytest.fixture
def company_context(client, db_session, TestSession, monkeypatch):
    user, first = _entity_fixture(client, db_session, TestSession, monkeypatch)
    other = ArkEntity(
        name="Second synthetic company",
        slug="second-company",
        entity_type="us_tx_llc",
        jurisdiction="US/TX",
        database_name="second_company",
        ark_files_path="/tmp/synthetic-second",
        status="active",
        posting_mode="assisted",
        facts_status="verified",
    )
    db_session.add(other)
    db_session.flush()
    db_session.add(ArkEntityAccess(entity_id=other.id, user_id=user.id, role="admin"))
    db_session.commit()
    engines = [
        create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        for _ in range(2)
    ]
    factories = {}
    for entity, engine in zip((first, other), engines):
        database.Base.metadata.create_all(engine)
        factories[entity.database_name] = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "entity_session_factory", factories.__getitem__)
    # Exercise the production control dependency too, including stale identity.
    monkeypatch.delitem(app.dependency_overrides, database.get_control_db)
    yield first, other, factories
    for engine in engines:
        engine.dispose()


def test_two_tab_headers_survive_other_tab_cookie_activation(client, company_context):
    first, other, factories = company_context
    assert (
        client.post(f"/api/ark-cpa/entities/{other.slug}/activate").status_code == 200
    )
    response = client.post(
        "/api/customers",
        headers={"X-Ark-Entity": first.slug, "X-Ark-Principal": "admin"},
        json={"name": "From first tab"},
    )
    assert response.status_code == 201, response.text
    with factories[first.database_name]() as ledger:
        assert ledger.query(Customer).one().name == "From first tab"
    with factories[other.database_name]() as ledger:
        assert ledger.query(Customer).count() == 0
    # Native links cannot attach headers. Their explicit URL wins over cookie.
    client.headers.pop("X-Ark-Entity")
    response = client.get(
        f"/api/customers?_ark_entity={first.slug}&_ark_principal=admin"
    )
    assert response.status_code == 200
    assert response.json()[0]["name"] == "From first tab"
    assert client.get(f"/api/customers?_ark_entity={other.slug}").json() == []


@pytest.mark.parametrize("suffix", ["", "?_ark_entity=synthetic-ledger"])
def test_mutation_cannot_fall_back_to_shared_cookie(client, company_context, suffix):
    client.headers.pop("X-Ark-Entity")
    response = client.post("/api/customers" + suffix, json={"name": "Must not save"})
    assert response.status_code == 400
    assert "explicit X-Ark-Entity" in response.json()["detail"]


@pytest.mark.parametrize(
    "path,headers,status",
    [
        ("/api/customers?_ark_entity=second-company", {}, 409),
        (
            "/api/customers?_ark_entity=synthetic-ledger&_ark_entity=second-company",
            {},
            400,
        ),
        ("/api/customers", {"X-Ark-Entity": "not-authorized"}, 403),
        ("/api/customers", {"X-Ark-Principal": "previous-user"}, 409),
        ("/api/ark-cpa/entities", {"X-Ark-Principal": "previous-user"}, 409),
        ("/api/customers?_ark_principal=previous-user", {}, 409),
    ],
)
def test_mismatched_company_or_identity_fails_closed(
    client, company_context, path, headers, status
):
    assert client.get(path, headers=headers).status_code == status


def test_context_and_recovery_require_real_authority(
    client, company_context, db_session
):
    assert client.get("/api/ark-cpa/ui-context").json() == {
        "entity_mode": True,
        "username": "admin",
    }
    assert client.get("/api/ark-cpa/recovery-status").status_code == 200
    db_session.query(ArkEntityAccess).update({"protected_approver": False})
    db_session.commit()
    assert client.get("/api/ark-cpa/recovery-status").status_code == 403


def test_readonly_and_frozen_controls_still_deny_writes(
    client, company_context, db_session
):
    first, _, _ = company_context
    first.posting_mode = "frozen"
    db_session.commit()
    assert client.post("/api/customers", json={"name": "Frozen"}).status_code == 409
    first.posting_mode = "assisted"
    db_session.query(ArkEntityAccess).filter_by(entity_id=first.id).update(
        {"role": "readonly"}
    )
    db_session.commit()
    assert client.post("/api/customers", json={"name": "Readonly"}).status_code == 403
    assert client.get("/api/customers").status_code == 200
