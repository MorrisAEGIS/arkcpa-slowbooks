#!/usr/bin/env python3
"""Run Ark CPA evidence, Bookkeeper, Controller, and tax-monitor cycles."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, entity_session_factory
from app.models.arkcpa import ArkAgentDecision, ArkEntity, ArkEvidence, ArkPostingCandidate
from app.services.arkcpa_controller import (
    post_candidate_if_eligible,
    run_candidate_review,
    run_evidence_bookkeeping,
)
from app.services.arkcpa_ingestion import scan_and_extract_entity
from app.services.arkcpa_tax import refresh_all_authority_sources


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("daily", "weekly", "monthly", "annual"), default="daily"
    )
    parser.add_argument("--entity", help="Limit the cycle to one entity slug")
    parser.add_argument("--apply", action="store_true", help="Persist the controller cycle")
    parser.add_argument(
        "--allow-posting",
        action="store_true",
        default=os.getenv("ARKCPA_CONTROLLER_ALLOW_POSTING", "false").lower()
        in {"1", "true", "yes"},
        help="Post only candidates that pass the autonomous routine-pattern gate",
    )
    parser.add_argument(
        "--refresh-tax",
        action="store_true",
        default=os.getenv("ARKCPA_CONTROLLER_REFRESH_TAX", "false").lower()
        in {"1", "true", "yes"},
    )
    parser.add_argument("--loop", action="store_true")
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("ARKCPA_CONTROLLER_INTERVAL_SECONDS", "900")),
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=int(os.getenv("ARKCPA_CONTROLLER_MAX_CANDIDATES", "25")),
    )
    return parser.parse_args()


def _entity_preview(control, entity: ArkEntity) -> dict:
    return {
        "entity_id": entity.id,
        "slug": entity.slug,
        "status": entity.status,
        "posting_mode": entity.posting_mode,
        "facts_status": entity.facts_status,
        "evidence": control.query(ArkEvidence).filter_by(entity_id=entity.id).count(),
        "review_queue": control.query(ArkPostingCandidate)
        .filter(
            ArkPostingCandidate.entity_id == entity.id,
            ArkPostingCandidate.status.in_(("draft", "reviewing", "blocked", "ready")),
        )
        .count(),
    }


def _run_entity(control, entity: ArkEntity, args: argparse.Namespace) -> dict:
    result = {"entity": entity.slug, "scan": None, "bookkeeping": [], "reviews": []}
    result["scan"] = scan_and_extract_entity(control, entity)
    with entity_session_factory(entity.database_name)() as ledger:
        evidence_rows = (
            control.query(ArkEvidence)
            .filter(ArkEvidence.entity_id == entity.id, ArkEvidence.status == "verified")
            .order_by(ArkEvidence.id)
            .limit(args.max_candidates)
            .all()
        )
        created_ids: set[int] = set()
        for evidence in evidence_rows:
            outcome = run_evidence_bookkeeping(
                control, ledger, evidence, run_type=args.mode
            )
            result["bookkeeping"].append(outcome)
            candidate_id = outcome.get("candidate_id")
            if candidate_id:
                created_ids.add(candidate_id)
                if args.allow_posting and outcome.get("gate_passed"):
                    candidate = control.get(ArkPostingCandidate, candidate_id)
                    outcome["posting"] = post_candidate_if_eligible(
                        control, ledger, candidate
                    )

        candidates = (
            control.query(ArkPostingCandidate)
            .filter(
                ArkPostingCandidate.entity_id == entity.id,
                ArkPostingCandidate.status.in_(("draft", "reviewing", "blocked")),
            )
            .order_by(ArkPostingCandidate.id)
            .limit(args.max_candidates)
            .all()
        )
        for candidate in candidates:
            if candidate.id in created_ids:
                continue
            current_roles = {
                row[0]
                for row in control.query(ArkAgentDecision.agent_role)
                .filter_by(candidate_id=candidate.id)
                .all()
            }
            if current_roles == {"bookkeeper", "controller"}:
                continue
            outcome = run_candidate_review(control, ledger, candidate)
            if args.allow_posting and outcome.get("gate_passed"):
                outcome["posting"] = post_candidate_if_eligible(
                    control, ledger, candidate
                )
            result["reviews"].append(outcome)
    return result


def run_cycle(args: argparse.Namespace) -> dict:
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "apply": args.apply,
        "allow_posting": args.allow_posting,
        "entities": [],
        "tax_sources": [],
    }
    with SessionLocal() as control:
        query = control.query(ArkEntity).filter(ArkEntity.status != "archived")
        if args.entity:
            query = query.filter(ArkEntity.slug == args.entity)
        entities = query.order_by(ArkEntity.id).all()
        if args.entity and not entities:
            raise ValueError("Requested Ark CPA entity was not found")
        if not args.apply:
            report["entities"] = [_entity_preview(control, entity) for entity in entities]
            return report
        for entity in entities:
            try:
                report["entities"].append(_run_entity(control, entity, args))
            except Exception as exc:
                control.rollback()
                report["entities"].append(
                    {"entity": entity.slug, "status": "failed", "error": str(exc)[:500]}
                )
        if args.refresh_tax:
            report["tax_sources"] = refresh_all_authority_sources(control)
    return report


def main() -> int:
    args = _args()
    if args.allow_posting and not args.apply:
        print("controller refused: --allow-posting requires --apply", file=sys.stderr)
        return 2
    if args.interval < 60:
        print("controller refused: --interval must be at least 60 seconds", file=sys.stderr)
        return 2
    while True:
        try:
            print(json.dumps(run_cycle(args), indent=2, sort_keys=True), flush=True)
        except Exception as exc:
            print(f"controller cycle failed: {exc}", file=sys.stderr, flush=True)
            if not args.loop:
                return 2
        if not args.loop:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
