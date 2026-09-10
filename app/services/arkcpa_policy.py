"""Deterministic second gate for agent-proposed Ark CPA journal entries."""

from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.models.accounts import Account
from app.models.arkcpa import (
    ArkAgentDecision,
    ArkEntity,
    ArkEvidence,
    ArkPostingCandidate,
)
from app.services.arkcpa_rules import (
    ENTITY_TYPES,
    MIN_AUTONOMOUS_CONFIDENCE,
    ORDINARY_ACTION,
    POLICY_VERSION,
    is_protected_action,
)
from app.services.closing_date import get_closing_date


def _amount(value) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Journal amounts must be valid decimal values") from exc


def evaluate_candidate(
    control_db: Session, ledger_db: Session, candidate: ArkPostingCandidate
) -> dict:
    """Return a proof-shaped decision; every check must pass for auto-posting."""
    entity = control_db.get(ArkEntity, candidate.entity_id)
    evidence = control_db.get(ArkEvidence, candidate.evidence_id)
    decisions = (
        control_db.query(ArkAgentDecision)
        .filter(ArkAgentDecision.candidate_id == candidate.id)
        .all()
    )
    by_role = {decision.agent_role: decision for decision in decisions}
    bookkeeper = by_role.get("bookkeeper")
    controller = by_role.get("controller")
    lines = candidate.lines if isinstance(candidate.lines, list) else []

    debits = Decimal("0")
    credits = Decimal("0")
    line_shape_ok = len(lines) >= 2
    account_ids: set[int] = set()
    try:
        for line in lines:
            if not isinstance(line, dict):
                line_shape_ok = False
                continue
            debit = _amount(line.get("debit"))
            credit = _amount(line.get("credit"))
            account_id = int(line.get("account_id"))
            account_ids.add(account_id)
            if debit < 0 or credit < 0 or (debit > 0) == (credit > 0):
                line_shape_ok = False
            debits += debit
            credits += credit
    except (ValueError, TypeError):
        line_shape_ok = False

    existing_ids = {
        row[0]
        for row in ledger_db.query(Account.id)
        .filter(Account.id.in_(account_ids or {-1}), Account.is_active)
        .all()
    }
    tax_code = (candidate.tax_context or {}).get("tax_code", "none")
    allowed_tax_codes = (
        ENTITY_TYPES.get(entity.entity_type, {}).get("tax_codes", ()) if entity else ()
    )
    bookkeeper_confidence = float(bookkeeper.confidence) if bookkeeper else 0.0
    controller_confidence = float(controller.confidence) if controller else 0.0
    closing_date = get_closing_date(ledger_db)

    checks = {
        "policy_version": POLICY_VERSION,
        "entity_active": bool(entity and entity.status == "active"),
        "entity_currency": bool(entity and candidate.currency == entity.currency),
        "verified_evidence": bool(
            evidence
            and evidence.entity_id == candidate.entity_id
            and evidence.status == "verified"
            and evidence.content_hash
        ),
        "ordinary_entry": candidate.action_type == ORDINARY_ACTION
        and not is_protected_action(candidate.action_type),
        "line_shape": line_shape_ok,
        "balanced": line_shape_ok and debits == credits and debits > 0,
        "accounts_exist_and_active": bool(account_ids) and existing_ids == account_ids,
        "recognized_tax_code": tax_code in allowed_tax_codes,
        "period_open": closing_date is None
        or candidate.transaction_date > closing_date,
        "bookkeeper_approved": bool(
            bookkeeper
            and bookkeeper.decision == "approve"
            and bookkeeper_confidence >= MIN_AUTONOMOUS_CONFIDENCE
        ),
        "controller_approved": bool(
            controller
            and controller.decision == "approve"
            and controller_confidence >= MIN_AUTONOMOUS_CONFIDENCE
        ),
        "independent_model_families": bool(
            bookkeeper
            and controller
            and bookkeeper.model_family.strip().lower()
            != controller.model_family.strip().lower()
        ),
        "current_policy_decisions": bool(
            bookkeeper
            and controller
            and bookkeeper.policy_version == POLICY_VERSION
            and controller.policy_version == POLICY_VERSION
        ),
        "decisions_match_evidence": bool(
            evidence
            and evidence.content_hash
            and bookkeeper
            and controller
            and bookkeeper.evidence_hash == evidence.content_hash
            and controller.evidence_hash == evidence.content_hash
        ),
    }
    failed = [
        name
        for name, passed in checks.items()
        if name != "policy_version" and not passed
    ]
    return {
        "passed": not failed,
        "failed": failed,
        "checks": checks,
        "totals": {"debits": str(debits), "credits": str(credits)},
        "threshold": MIN_AUTONOMOUS_CONFIDENCE,
    }
