"""Offline Ark CPA entity-provisioning safety checks."""

from contextlib import nullcontext

import pytest

from app.models.arkcpa import ArkEntity, ArkEntityAccess
from app.models.users import User
from scripts.provision_arkcpa_entities import _ensure_dirs, _provision, _validate


def _entity(**changes):
    row = {
        "name": "Stage Alberta Corporation",
        "slug": "stage-alberta-corp",
        "entity_type": "ca_ab_corporation",
        "database_name": "stage_alberta_corp",
        "jurisdiction": "Canada / Alberta",
        "currency": "CAD",
        "fiscal_year_end_month": 12,
        "fiscal_year_end_day": 31,
        "access": {
            "jay": {"role": "admin", "protected_approver": True},
            "maesa": {"role": "admin", "protected_approver": False},
        },
    }
    row.update(changes)
    return row


def test_entity_config_requires_valid_role_and_calendar_date():
    with pytest.raises(ValueError, match="Invalid access roles"):
        _validate(
            _entity(
                access={
                    "jay": {"role": "admin", "protected_approver": True},
                    "maesa": {"role": "owner", "protected_approver": False},
                }
            )
        )
    with pytest.raises(ValueError, match="valid month and day"):
        _validate(_entity(fiscal_year_end_month=2, fiscal_year_end_day=31))


def test_entity_config_refuses_source_control_placeholders():
    with pytest.raises(ValueError, match="exact legal names"):
        _validate(_entity(name="EXACT_ALBERTA_CORPORATION_NAME"))


def test_entity_config_requires_one_admin_protected_approver():
    with pytest.raises(ValueError, match="exactly one protected approver"):
        _validate(
            _entity(
                access={
                    "jay": {"role": "admin", "protected_approver": False},
                    "maesa": {"role": "admin", "protected_approver": False},
                }
            )
        )


def test_entity_config_validates_governance_and_default_user():
    with pytest.raises(ValueError, match="Invalid facts status"):
        _validate(_entity(facts_status="trusted"))
    with pytest.raises(ValueError, match="Default user must be in the access map"):
        _validate(_entity(default_user="someone-else"))
    with pytest.raises(ValueError, match="exactly one protected approver"):
        _validate(
            _entity(
                access={
                    "jay": {"role": "admin", "protected_approver": True},
                    "maesa": {"role": "admin", "protected_approver": True},
                }
            )
        )


def test_ark_files_root_refuses_symlink_entity_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    entity_root = tmp_path / "_shared" / "Ark CPA"
    entity_root.mkdir(parents=True)
    (entity_root / "stage-alberta-corp").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="cannot be a symlink"):
        _ensure_dirs(tmp_path, "stage-alberta-corp", apply=True)


def test_ark_files_dry_run_is_non_mutating(tmp_path):
    target = _ensure_dirs(tmp_path, "stage-alberta-corp", apply=False)
    assert target == tmp_path / "_shared" / "Ark CPA" / "stage-alberta-corp"
    assert not target.exists()


def test_existing_entity_is_updated_and_protected_owner_is_reconciled(
    db_session, tmp_path, monkeypatch
):
    jay = User(
        username="jay", display_name="Jay", password_hash="hash", role="admin"
    )
    maesa = User(
        username="maesa", display_name="Maesa", password_hash="hash", role="admin"
    )
    prior = User(
        username="prior", display_name="Prior", password_hash="hash", role="admin"
    )
    db_session.add_all([jay, maesa, prior])
    db_session.flush()
    entity = ArkEntity(
        name="Old Name",
        slug="stage-alberta-corp",
        entity_type="ca_ab_corporation",
        jurisdiction="Canada / Alberta",
        status="active",
        database_name="stage_alberta_corp",
        ark_files_path="_shared/Ark CPA/stage-alberta-corp",
        currency="CAD",
        fiscal_year_end_month=6,
        fiscal_year_end_day=30,
        posting_mode="draft_only",
        facts_status="incomplete",
        profile={},
    )
    db_session.add(entity)
    db_session.flush()
    db_session.add_all(
        [
            ArkEntityAccess(
                entity_id=entity.id,
                user_id=jay.id,
                role="admin",
                protected_approver=False,
            ),
            ArkEntityAccess(
                entity_id=entity.id,
                user_id=maesa.id,
                role="bookkeeper",
                protected_approver=False,
            ),
            ArkEntityAccess(
                entity_id=entity.id,
                user_id=prior.id,
                role="admin",
                protected_approver=True,
            ),
        ]
    )
    db_session.commit()

    monkeypatch.setattr(
        "scripts.provision_arkcpa_entities.entity_session_factory",
        lambda _database: lambda: nullcontext(db_session),
    )
    monkeypatch.setattr(
        "scripts.provision_arkcpa_entities.set_setting", lambda *_args: None
    )
    monkeypatch.setattr(
        "scripts.provision_arkcpa_entities.create_company",
        lambda *_args: pytest.fail("existing ledger must not be recreated"),
    )

    result = _provision(
        db_session,
        _entity(default_user="jay", posting_mode="assisted"),
        tmp_path,
        True,
    )

    db_session.refresh(entity)
    accesses = {
        row.user_id: row
        for row in db_session.query(ArkEntityAccess).filter_by(entity_id=entity.id)
    }
    assert result["status"] == "updated"
    assert entity.name == "Stage Alberta Corporation"
    assert entity.fiscal_year_end_month == 12
    assert entity.posting_mode == "assisted"
    assert accesses[jay.id].role == "admin"
    assert accesses[jay.id].protected_approver is True
    assert accesses[jay.id].is_default is True
    assert accesses[maesa.id].role == "admin"
    assert accesses[maesa.id].protected_approver is False
    assert accesses[prior.id].role == "admin"
    assert accesses[prior.id].protected_approver is False
