from datetime import date
from decimal import Decimal

from app.models.accounts import Account, AccountType
from app.models.transactions import Transaction, TransactionLine


def _post(db, when, source_type, *lines):
    transaction = Transaction(date=when, source_type=source_type)
    db.add(transaction)
    db.flush()
    for account, debit, credit in lines:
        db.add(
            TransactionLine(
                transaction_id=transaction.id,
                account_id=account.id,
                debit=Decimal(debit),
                credit=Decimal(credit),
            )
        )


def test_cash_flow_matches_linked_cash_change_for_native_journals(client, db_session):
    checking = Account(
        name="Checking", account_type=AccountType.ASSET, bank_kind="bank"
    )
    savings = Account(name="Savings", account_type=AccountType.ASSET, bank_kind="bank")
    equipment = Account(name="Equipment", account_type=AccountType.ASSET)
    payable = Account(name="Accounts payable", account_type=AccountType.LIABILITY)
    loan = Account(name="Term loan", account_type=AccountType.LIABILITY)
    equity = Account(name="Owner equity", account_type=AccountType.EQUITY)
    revenue = Account(name="Service revenue", account_type=AccountType.INCOME)
    expense = Account(name="Operating expense", account_type=AccountType.EXPENSE)
    db_session.add_all(
        [checking, savings, equipment, payable, loan, equity, revenue, expense]
    )
    db_session.flush()

    # Carried state belongs in the opening cash balance, not period cash flow.
    _post(
        db_session,
        date(2026, 1, 1),
        "opening_balance",
        (checking, "1000", "0"),
        (equity, "0", "1000"),
    )
    _post(
        db_session,
        date(2026, 1, 2),
        "sales_receipt",
        (checking, "500", "0"),
        (revenue, "0", "500"),
    )
    _post(
        db_session,
        date(2026, 1, 3),
        "expense",
        (expense, "120", "0"),
        (checking, "0", "120"),
    )
    _post(
        db_session,
        date(2026, 1, 4),
        "journal",
        (equipment, "200", "0"),
        (checking, "0", "200"),
    )
    _post(
        db_session,
        date(2026, 1, 5),
        "journal",
        (checking, "300", "0"),
        (loan, "0", "300"),
    )
    _post(
        db_session,
        date(2026, 1, 6),
        "journal",
        (equity, "50", "0"),
        (checking, "0", "50"),
    )
    # Non-cash accruals and transfers between linked cash accounts have no
    # effect on total cash and must not leak into the statement.
    _post(
        db_session,
        date(2026, 1, 7),
        "bill",
        (expense, "40", "0"),
        (payable, "0", "40"),
    )
    _post(
        db_session,
        date(2026, 1, 8),
        "transfer",
        (savings, "75", "0"),
        (checking, "0", "75"),
    )
    db_session.commit()

    response = client.get(
        "/api/reports/cash-flow",
        params={"start_date": "2026-01-01", "end_date": "2026-01-31"},
    )

    assert response.status_code == 200
    report = response.json()
    assert report["total_operating"] == 380.0, report
    assert report["total_investing"] == -200.0
    assert report["total_financing"] == 250.0
    assert report["net_change"] == 430.0
