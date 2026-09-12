# ============================================================================
# Bank rule application — shared by every bank-feed importer (OFX, CSV,
# SimpleFIN) and the Bank Rules page.
#
# A rule SUGGESTS a category for a statement line that matches its payee
# pattern. It never changes the line's status and never posts: adding to
# the books is one explicit click ("Add" / "Add all categorised"), so
# nothing reaches the ledger silently (issue #114).
# ============================================================================

import logging

from sqlalchemy.orm import Session

from app.models.banking import BankTransaction

logger = logging.getLogger(__name__)


def _hit(rule, payee: str) -> bool:
    pattern = (rule.pattern or "").lower()
    if not pattern:
        return False
    if rule.rule_type == "contains":
        return pattern in payee
    if rule.rule_type == "starts_with":
        return payee.startswith(pattern)
    if rule.rule_type == "exact":
        return payee == pattern
    return False


def apply_bank_rules(db: Session, bank_account_id: int | None = None) -> int:
    """Categorise unmatched statement lines by the active rules (highest
    priority first, first hit wins). Returns how many lines got a category.
    Commits when anything changed."""
    from app.models.bank_rules import BankRule

    rules = (
        db.query(BankRule)
        .filter(BankRule.is_active)
        .order_by(BankRule.priority.desc())
        .all()
    )
    if not rules:
        return 0
    q = db.query(BankTransaction).filter(BankTransaction.match_status == "unmatched")
    if bank_account_id:
        q = q.filter(BankTransaction.bank_account_id == bank_account_id)
    categorised = 0
    for txn in q.all():
        payee = (txn.payee or "").lower()
        for rule in rules:
            if _hit(rule, payee):
                if rule.account_id and txn.category_account_id != rule.account_id:
                    txn.category_account_id = rule.account_id
                    categorised += 1
                break
    if categorised:
        db.commit()
    return categorised
