"""A stale identity map must not overwrite another journal's account balances."""

from datetime import date
from decimal import Decimal

from app.models.accounts import Account
from app.services.accounting import create_journal_entry


def test_post_preserves_another_sessions_committed_balances(
    db_session, TestSession, seed_accounts
):
    cash = seed_accounts["1000"]
    income = seed_accounts["4000"]
    # Load and retain the original account objects, as invoice resolution does.
    assert cash.balance == Decimal("0")
    assert income.balance == Decimal("0")

    def lines(amount):
        return [
            {"account_id": cash.id, "debit": amount, "credit": 0},
            {"account_id": income.id, "debit": 0, "credit": amount},
        ]

    with TestSession() as other:
        create_journal_entry(other, date(2026, 10, 1), "Other synthetic sale", lines(7))
        other.commit()
    assert cash.balance == Decimal("0")  # first session still has an old snapshot
    create_journal_entry(db_session, date(2026, 10, 1), "This synthetic sale", lines(3))
    db_session.commit()
    with TestSession() as verify:
        assert verify.get(Account, cash.id).balance == Decimal("10")
        assert verify.get(Account, income.id).balance == Decimal("10")


def test_repeated_account_lines_and_sequential_posts_keep_current_balance(
    db_session, seed_accounts
):
    cash = seed_accounts["1000"]
    income = seed_accounts["4000"]
    create_journal_entry(
        db_session,
        date(2026, 10, 1),
        "Split synthetic sale",
        [
            {"account_id": cash.id, "debit": 3},
            {"account_id": cash.id, "debit": 4},
            {"account_id": income.id, "credit": 7},
        ],
    )
    assert cash.balance == Decimal("7")
    create_journal_entry(
        db_session,
        date(2026, 10, 1),
        "Synthetic reversal",
        [{"account_id": cash.id, "credit": 2}, {"account_id": income.id, "debit": 2}],
    )
    db_session.commit()
    assert cash.balance == income.balance == Decimal("5")
