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
from app.services.arkcpa_rules import ENTITY_TYPES, obligations_for
from app.services.company_service import create_company
from app.services.settings_service import set_setting

_SLUG = re.compile(r"^[a-z][a-z0-9-]{1,78}[a-z0-9]$")
_DATABASE = re.compile(r"^[a-z][a-z0-9_]{1,61}[a-z0-9]$")


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
    if not isinstance(item["access"], dict) or not item["access"]:
        raise ValueError("At least one user access grant is required")
    invalid_roles = sorted(set(item["access"].values()) - set(VALID_ROLES))
    if invalid_roles:
        raise ValueError(f"Invalid access roles: {', '.join(invalid_roles)}")
    if "admin" not in item["access"].values():
        raise ValueError("Each entity requires an admin access grant")
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
    if existing:
        return {"slug": item["slug"], "status": "exists", "ark_files": str(target)}
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
            "status": "would-create",
            "ark_files": str(target),
        }

    result = create_company(
        db,
        item["name"],
        item["database_name"],
        f"Ark CPA {item['entity_type']}",
    )
    if not result.get("success"):
        raise RuntimeError(result.get("error", "Ledger database provisioning failed"))
    with entity_session_factory(item["database_name"])() as ledger:
        set_setting(ledger, "company_name", item["name"])
        set_setting(ledger, "home_currency", item["currency"])
        ledger.commit()
    entity = ArkEntity(
        name=item["name"],
        slug=item["slug"],
        entity_type=item["entity_type"],
        jurisdiction=item["jurisdiction"],
        status=item.get("status", "active"),
        database_name=item["database_name"],
        ark_files_path=f"_shared/Ark CPA/{item['slug']}",
        currency=item["currency"],
        fiscal_year_end_month=int(item.get("fiscal_year_end_month", 12)),
        fiscal_year_end_day=int(item.get("fiscal_year_end_day", 31)),
        payroll_enabled=False,
    )
    db.add(entity)
    db.flush()
    default_user = item.get("default_user")
    if default_user:
        default = users.get(default_user)
        if default is None:
            raise ValueError(f"Default user is not in access map: {default_user}")
        db.query(ArkEntityAccess).filter(ArkEntityAccess.user_id == default.id).update(
            {ArkEntityAccess.is_default: False}
        )
    for username, role in item["access"].items():
        db.add(
            ArkEntityAccess(
                entity_id=entity.id,
                user_id=users[username].id,
                role=role,
                is_default=username == default_user,
            )
        )
    for obligation in obligations_for(entity.entity_type):
        db.add(ArkComplianceObligation(entity_id=entity.id, **obligation))
    db.commit()
    return {
        "slug": entity.slug,
        "status": "created",
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
