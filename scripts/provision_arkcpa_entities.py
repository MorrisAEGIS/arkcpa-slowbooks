#!/usr/bin/env python3
"""Provision Ark CPA legal-entity ledgers and Ark Files folders.

Dry-run is the default.  Exact legal names belong in a private config file,
not in source control or model prompts.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, entity_session_factory
from app.models.arkcpa import ArkComplianceObligation, ArkEntity, ArkEntityAccess
from app.models.users import User
from app.models.users import VALID_ROLES
from app.services.ark_files import FOLDERS
from app.services.arkcpa_rules import (
    ENTITY_TYPES,
    obligations_for,
    validate_entity_profile,
)
from app.services.company_service import create_company
from app.services.settings_service import set_setting

_SLUG = re.compile(r"^[a-z][a-z0-9-]{1,78}[a-z0-9]$")
_DATABASE = re.compile(r"^[a-z][a-z0-9_]{1,61}[a-z0-9]$")


def _access_grants(item: dict) -> dict[str, dict]:
    grants = {}
    for username, value in item["access"].items():
        if isinstance(value, str):
            grants[username] = {"role": value, "protected_approver": False}
        elif isinstance(value, dict):
            grants[username] = {
                "role": value.get("role"),
                "protected_approver": bool(value.get("protected_approver", False)),
            }
        else:
            raise ValueError(f"Invalid access grant for {username}")
    return grants


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--ark-files-root",
        type=Path,
        default=Path(os.getenv("ARK_FILES_ROOT_HOST", "/home/novaadmin/ark-files")),
    )
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def _load(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("entities"), list):
        raise ValueError("Config must contain an entities array")
    return data["entities"]


def _validate(item: dict) -> None:
    required = {
        "name",
        "slug",
        "entity_type",
        "database_name",
        "jurisdiction",
        "currency",
        "access",
    }
    missing = sorted(required - item.keys())
    if missing:
        raise ValueError(f"Missing fields: {', '.join(missing)}")
    if "EXACT_" in item["name"]:
        raise ValueError("Replace placeholder names with exact legal names")
    if not _SLUG.fullmatch(item["slug"]):
        raise ValueError(f"Invalid slug: {item['slug']}")
    if not _DATABASE.fullmatch(item["database_name"]):
        raise ValueError(f"Invalid database name: {item['database_name']}")
    if item["entity_type"] not in ENTITY_TYPES:
        raise ValueError(f"Invalid entity type: {item['entity_type']}")
    if item["currency"] != ENTITY_TYPES[item["entity_type"]]["currency"]:
        raise ValueError(f"Currency does not match {item['entity_type']}")
    posting_mode = item.get(
        "posting_mode",
        "draft_only" if item.get("status", "active") == "dormant" else "assisted",
    )
    if item.get("status", "active") not in {"active", "dormant"}:
        raise ValueError("Invalid entity status")
    if posting_mode not in {"draft_only", "assisted", "autopost_ordinary", "frozen"}:
        raise ValueError("Invalid posting mode")
    if (
        item.get("status", "active") == "dormant"
        and posting_mode == "autopost_ordinary"
    ):
        raise ValueError("Dormant entities cannot enable autonomous posting")
    if item.get("facts_status", "incomplete") not in {"incomplete", "verified", "hold"}:
        raise ValueError("Invalid facts status")
    validate_entity_profile(item.get("profile", {}))
    if not isinstance(item["access"], dict) or not item["access"]:
        raise ValueError("At least one user access grant is required")
    grants = _access_grants(item)
    invalid_roles = sorted(
        {grant["role"] for grant in grants.values()} - set(VALID_ROLES)
    )
    if invalid_roles:
        raise ValueError(f"Invalid access roles: {', '.join(invalid_roles)}")
    if "admin" not in {grant["role"] for grant in grants.values()}:
        raise ValueError("Each entity requires an admin access grant")
    protected = [
        username for username, grant in grants.items() if grant["protected_approver"]
    ]
    if len(protected) != 1:
        raise ValueError("Each entity requires exactly one protected approver")
    if grants[protected[0]]["role"] != "admin":
        raise ValueError("The protected approver must be an entity admin")
    if item.get("default_user") and item["default_user"] not in grants:
        raise ValueError("Default user must be in the access map")
    try:
        datetime(
            2000,
            int(item.get("fiscal_year_end_month", 12)),
            int(item.get("fiscal_year_end_day", 31)),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Fiscal year end must be a valid month and day") from exc


def _ensure_dirs(root: Path, slug: str, apply: bool) -> Path:
    root = root.resolve()
    entity_root = root / "_shared" / "Ark CPA" / slug
    resolved = entity_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("Ark Files entity path escapes the configured root")
    if entity_root.is_symlink():
        raise ValueError("Ark Files entity root cannot be a symlink")
    if apply:
        resolved.mkdir(parents=True, exist_ok=True, mode=0o750)
        for folder in FOLDERS:
            (resolved / folder).mkdir(exist_ok=True, mode=0o750)
    return resolved


def _provision(db, item: dict, root: Path, apply: bool) -> dict:
    _validate(item)
    target = _ensure_dirs(root, item["slug"], apply)
    existing = db.query(ArkEntity).filter(ArkEntity.slug == item["slug"]).first()
    users = {
        row.username: row
        for row in db.query(User)
        .filter(User.username.in_(item["access"].keys()), User.is_active)
        .all()
    }
    absent = sorted(set(item["access"]) - set(users))
    if absent:
        raise ValueError(f"Active Ark CPA users not found: {', '.join(absent)}")
    if not apply:
        return {
            "slug": item["slug"],
            "status": "would-update" if existing else "would-create",
            "ark_files": str(target),
        }

    expected_path = f"_shared/Ark CPA/{item['slug']}"
    if existing:
        if existing.database_name != item["database_name"]:
            raise ValueError("Existing entity database name does not match config")
        if existing.ark_files_path != expected_path:
            raise ValueError("Existing entity Ark Files path does not match config")
        entity = existing
        result_status = "updated"
    else:
        result = create_company(
            db,
            item["name"],
            item["database_name"],
            f"Ark CPA {item['entity_type']}",
        )
        if not result.get("success"):
            raise RuntimeError(
                result.get("error", "Ledger database provisioning failed")
            )
        entity = ArkEntity(slug=item["slug"], database_name=item["database_name"])
        db.add(entity)
        result_status = "created"

    entity.name = item["name"]
    entity.entity_type = item["entity_type"]
    entity.jurisdiction = item["jurisdiction"]
    entity.status = item.get("status", "active")
    entity.ark_files_path = expected_path
    entity.currency = item["currency"]
    entity.fiscal_year_end_month = int(item.get("fiscal_year_end_month", 12))
    entity.fiscal_year_end_day = int(item.get("fiscal_year_end_day", 31))
    entity.payroll_enabled = False
    entity.posting_mode = item.get(
        "posting_mode",
        "draft_only" if item.get("status", "active") == "dormant" else "assisted",
    )
    entity.facts_status = item.get("facts_status", "incomplete")
    entity.profile = item.get("profile", {})
    db.flush()

    with entity_session_factory(item["database_name"])() as ledger:
        set_setting(ledger, "company_name", item["name"])
        set_setting(ledger, "home_currency", item["currency"])
        ledger.commit()

    default_user = item.get("default_user")
    if default_user:
        default = users[default_user]
        db.query(ArkEntityAccess).filter(ArkEntityAccess.user_id == default.id).update(
            {ArkEntityAccess.is_default: False}
        )
    grants = _access_grants(item)
    # The config is authoritative for the protected gate. Preserve any
    # unlisted user's ordinary access, but ensure only the configured human
    # can approve filings, payments, credentials, close, payroll, or promotion.
    db.query(ArkEntityAccess).filter(ArkEntityAccess.entity_id == entity.id).update(
        {ArkEntityAccess.protected_approver: False}
    )
    for username, grant in grants.items():
        access = (
            db.query(ArkEntityAccess)
            .filter_by(entity_id=entity.id, user_id=users[username].id)
            .first()
        )
        if access is None:
            access = ArkEntityAccess(entity_id=entity.id, user_id=users[username].id)
            db.add(access)
        access.role = grant["role"]
        access.is_default = username == default_user
        access.protected_approver = grant["protected_approver"]
    for obligation in obligations_for(entity.entity_type):
        row = (
            db.query(ArkComplianceObligation)
            .filter_by(
                entity_id=entity.id,
                code=obligation["code"],
                tax_year=obligation["tax_year"],
            )
            .first()
        )
        if row is None:
            row = ArkComplianceObligation(entity_id=entity.id)
            db.add(row)
        for key, value in obligation.items():
            setattr(row, key, value)
    db.commit()
    return {
        "slug": entity.slug,
        "status": result_status,
        "database": entity.database_name,
        "ark_files": str(target),
    }


def main() -> int:
    args = _args()
    try:
        rows = _load(args.config)
        results = []
        with SessionLocal() as db:
            for item in rows:
                results.append(
                    _provision(db, item, args.ark_files_root.resolve(), args.apply)
                )
        print(
            json.dumps(
                {
                    "mode": "apply" if args.apply else "dry-run",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "entities": results,
                },
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        print(f"provisioning refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
