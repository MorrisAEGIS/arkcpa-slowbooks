"""Offline Ark CPA entity-provisioning safety checks."""

import pytest

from scripts.provision_arkcpa_entities import _ensure_dirs, _validate


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
        "access": {"jay": "admin", "maesa": "bookkeeper"},
    }
    row.update(changes)
    return row


def test_entity_config_requires_valid_role_and_calendar_date():
    with pytest.raises(ValueError, match="Invalid access roles"):
        _validate(_entity(access={"jay": "admin", "maesa": "owner"}))
    with pytest.raises(ValueError, match="valid month and day"):
        _validate(_entity(fiscal_year_end_month=2, fiscal_year_end_day=31))


def test_entity_config_refuses_source_control_placeholders():
    with pytest.raises(ValueError, match="exact legal names"):
        _validate(_entity(name="EXACT_ALBERTA_CORPORATION_NAME"))


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
