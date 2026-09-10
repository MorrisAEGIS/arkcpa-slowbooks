#!/usr/bin/env python3
"""Migrate every registered Ark CPA ledger before the web process starts."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal
from app.models.arkcpa import ArkEntity
from app.services.company_service import _init_company_db, get_company_db_url
from app.services.settings_service import upgrade_plaintext_secrets


def main() -> int:
    results = []
    try:
        with SessionLocal() as control:
            entities = (
                control.query(ArkEntity)
                .filter(ArkEntity.status != "archived")
                .order_by(ArkEntity.id)
                .all()
            )
            for entity in entities:
                _init_company_db(get_company_db_url(entity.database_name))
                from app.database import entity_session_factory

                with entity_session_factory(entity.database_name)() as ledger:
                    upgraded_secrets = upgrade_plaintext_secrets(ledger)
                results.append(
                    {
                        "slug": entity.slug,
                        "database": entity.database_name,
                        "status": "migrated",
                        "secrets_upgraded": upgraded_secrets,
                    }
                )
        print(json.dumps({"entity_ledgers": results}, indent=2))
        return 0
    except Exception as exc:
        print(f"Ark CPA entity migration refused startup: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
