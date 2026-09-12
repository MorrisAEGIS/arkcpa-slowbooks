"""Canonical Ark CPA Authentik provisioning safety contract."""

import argparse
import stat

import pytest

from deploy.provision_authentik_oidc import (
    _read_env,
    _validate,
    _workspace_env,
    _write_env,
)


def _args(tmp_path, **changes):
    values = {
        "env_file": tmp_path / "private" / "arkcpa.env",
        "slug": "arkcpa",
        "hostname": "arkcpa.magaenergy.ai",
        "alias": [],
        "workspace_id": "arkcpa",
        "compose_project": "arkcpa-slowbooks",
        "postgres_user": "arkcpa",
        "postgres_db": "arkcpa",
        "publish_port": 3334,
        "bootstrap_email": "owner@magaenergy.ai",
        "group": "ArkCPA Owners",
        "workspace_label": "Multi-entity books",
        "company_name": "Ark CPA",
    }
    values.update(changes)
    return argparse.Namespace(**values)


def _oidc():
    return {
        "issuer": "https://auth.example/application/o/arkcpa/",
        "client_id": "client-id",
        "client_secret": "client-secret",
    }


def test_new_workspace_env_has_independent_secrets_and_private_permissions(tmp_path):
    args = _args(tmp_path)
    _validate(args)
    values = _workspace_env(args, _oidc())
    _write_env(args.env_file, values)

    written = _read_env(args.env_file)
    assert written["COMPOSE_PROJECT_NAME"] == "arkcpa-slowbooks"
    assert written["APP_PUBLISH_PORT"] == "3334"
    assert written["AUTHENTIK_OIDC_REQUIRED_GROUP"] == "ArkCPA Owners"
    assert written["AUTHENTIK_OIDC_BOOTSTRAP_EMAIL"] == "owner@magaenergy.ai"
    assert written["ARKCPA_WORKSPACE_ID"] == "arkcpa"
    assert written["ARKCPA_PUBLIC_HOSTNAME"] == "arkcpa.magaenergy.ai"
    assert written["POSTGRES_PASSWORD"]
    assert written["SESSION_SECRET_KEY"]
    assert written["PAYROLL_ENCRYPTION_SECRET"]
    assert written["SETTINGS_ENCRYPTION_KEY"]
    assert (
        len(
            {
                written["POSTGRES_PASSWORD"],
                written["SESSION_SECRET_KEY"],
                written["PAYROLL_ENCRYPTION_SECRET"],
                written["SETTINGS_ENCRYPTION_KEY"],
            }
        )
        == 4
    )
    assert stat.S_IMODE(args.env_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(args.env_file.parent.stat().st_mode) == 0o700


def test_rerun_preserves_existing_workspace_secrets(tmp_path):
    args = _args(tmp_path)
    original = {
        "POSTGRES_PASSWORD": "database-secret",
        "PAYROLL_ENCRYPTION_SECRET": "payroll-secret",
        "SESSION_SECRET_KEY": "session-secret",
        "SETTINGS_ENCRYPTION_KEY": "settings-secret",
    }
    _write_env(args.env_file, original)

    values = _workspace_env(args, _oidc())
    for key, expected in original.items():
        assert values[key] == expected


def test_unsafe_hostname_or_alias_fails_before_authentik_mutation(tmp_path):
    with pytest.raises(SystemExit, match="hostname"):
        _validate(_args(tmp_path, hostname="bad host"))
    with pytest.raises(SystemExit, match="alias"):
        _validate(_args(tmp_path, alias=["https://not-a-host/"]))
