"""CSV bank import: format auto-detection, PayPal Gross/Fee handling,
content-derived import_id dedup, and bank-rule parity with OFX import."""

from decimal import Decimal

from app.models.accounts import Account, AccountType
from app.models.bank_rules import BankRule
from app.models.banking import BankAccount, BankTransaction
from app.services.bank_csv_import import (
    detect_format,
    import_csv_transactions,
    parse_csv,
)

CHASE_CHECKING_CSV = (
    "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
    "DEBIT,07/01/2026,COFFEE SHOP,-4.50,DEBIT_CARD,995.50,\n"
    "DEBIT,07/01/2026,COFFEE SHOP,-4.50,DEBIT_CARD,991.00,\n"
    "CREDIT,07/02/2026,PAYROLL DEPOSIT,2500.00,ACH_CREDIT,3491.00,\n"
    "DEBIT,07/03/2026,CHECK 1041,-125.00,CHECK_PAID,3366.00,1041\n"
)

CHASE_CREDIT_CSV = (
    "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
    "07/05/2026,07/06/2026,HARDWARE STORE,Home,Sale,-89.99,\n"
    "07/07/2026,07/08/2026,PAYMENT THANK YOU,,Payment,500.00,\n"
)

PAYPAL_OLD_CSV = (
    '﻿"Date","Time","Time Zone","Name","Type","Status","Currency","Gross","Fee","Net",'
    '"From Email Address","To Email Address","Transaction ID","Item Title"\n'
    '"07/10/2026","10:00:00","PDT","Ada Lovelace","Express Checkout Payment","Completed",'
    '"USD","100.00","-3.20","96.80","ada@example.com","me@example.com","TX123","Widget"\n'
    '"07/10/2026","10:00:05","PDT","","Bank Deposit to PP Account","Completed",'
    '"USD","-96.80","0.00","-96.80","","","TX124",""\n'
)

PAYPAL_NEW_CSV = (
    "Date,Time,Time Zone,Description,Currency,Gross,Fee,Net,Balance,Transaction ID,"
    "From Email Address,Name,Bank Name,Bank Account,Shipping and Handling Amount,"
    "Sales Tax,Invoice ID,Reference Txn ID\n"
    "07/12/2026,09:00:00,PDT,General Payment,USD,250.00,-7.55,242.45,242.45,TX200,"
    "grace@example.com,Grace Hopper,,,0.00,0.00,INV-9,\n"
)


def _mk_bank_account(db_session):
    ba = BankAccount(name="Test Checking", bank_name="Chase")
    db_session.add(ba)
    db_session.commit()
    return ba


# ── Format detection ─────────────────────────────────────────────────────


def test_detect_formats():
    assert (
        detect_format(
            {"Details", "Posting Date", "Description", "Amount", "Type", "Balance"}
        )
        == "chase_checking"
    )
    assert (
        detect_format(
            {
                "Transaction Date",
                "Post Date",
                "Description",
                "Category",
                "Type",
                "Amount",
            }
        )
        == "chase_credit"
    )
    assert detect_format({"Date", "Time", "Name", "Type", "Status"}) == "paypal"
    assert detect_format({"Nothing", "Useful"}) == "unknown"


def test_parse_chase_checking_rows():
    result = parse_csv(CHASE_CHECKING_CSV)
    assert result["format"] == "chase_checking"
    assert result["error"] is None
    txns = result["transactions"]
    assert len(txns) == 4
    assert txns[0]["amount"] == Decimal("-4.50")
    assert txns[3]["check_number"] == "1041"


def test_parse_paypal_gross_fee_and_mirror_skip():
    result = parse_csv(PAYPAL_OLD_CSV)
    assert result["format"] == "paypal"
    txns = result["transactions"]
    # The "Bank Deposit to PP Account" mirror row is skipped
    assert len(txns) == 1
    # Gross, NOT Net
    assert txns[0]["amount"] == Decimal("100.00")
    assert txns[0]["fee"] == Decimal("-3.20")


def test_parse_paypal_new_format():
    result = parse_csv(PAYPAL_NEW_CSV)
    assert result["format"] == "paypal_new"
    txns = result["transactions"]
    assert len(txns) == 1
    assert txns[0]["amount"] == Decimal("250.00")
    assert txns[0]["payee"] == "Grace Hopper"


# ── Import + dedup ───────────────────────────────────────────────────────


def test_same_day_same_amount_duplicates_both_import(db_session):
    """Two identical coffee charges on the same day are BOTH real
    transactions — the (date, amount) dedup this replaces dropped one."""
    ba = _mk_bank_account(db_session)
    result = import_csv_transactions(db_session, ba.id, CHASE_CHECKING_CSV)
    assert result["imported"] == 4
    assert result["skipped"] == 0
    coffee = (
        db_session.query(BankTransaction)
        .filter(BankTransaction.payee == "COFFEE SHOP")
        .all()
    )
    assert len(coffee) == 2


def test_reimport_same_file_skips_everything(db_session):
    ba = _mk_bank_account(db_session)
    first = import_csv_transactions(db_session, ba.id, CHASE_CHECKING_CSV)
    assert first["imported"] == 4
    second = import_csv_transactions(db_session, ba.id, CHASE_CHECKING_CSV)
    assert second["imported"] == 0
    assert second["skipped"] == 4


def test_overlapping_export_skips_only_known_rows(db_session):
    """A later export containing already-imported rows plus new ones
    imports only the new ones."""
    ba = _mk_bank_account(db_session)
    import_csv_transactions(db_session, ba.id, CHASE_CHECKING_CSV)

    overlapping = CHASE_CHECKING_CSV + (
        "DEBIT,07/04/2026,GROCERY STORE,-62.10,DEBIT_CARD,3303.90,\n"
    )
    result = import_csv_transactions(db_session, ba.id, overlapping)
    assert result["imported"] == 1
    assert result["skipped"] == 4


def test_import_source_tagged_with_format(db_session):
    ba = _mk_bank_account(db_session)
    import_csv_transactions(db_session, ba.id, CHASE_CREDIT_CSV)
    sources = {
        t.import_source
        for t in db_session.query(BankTransaction)
        .filter(BankTransaction.bank_account_id == ba.id)
        .all()
    }
    assert sources == {"csv_chase_credit"}


def test_paypal_fee_surfaces_in_description(db_session):
    ba = _mk_bank_account(db_session)
    import_csv_transactions(db_session, ba.id, PAYPAL_OLD_CSV)
    txn = (
        db_session.query(BankTransaction)
        .filter(BankTransaction.bank_account_id == ba.id)
        .one()
    )
    assert "fee -3.20" in txn.description


# ── Bank-rule parity with OFX ────────────────────────────────────────────


def test_bank_rules_auto_apply_on_csv_import(db_session):
    ba = _mk_bank_account(db_session)
    expense = Account(name="Meals", account_type=AccountType.EXPENSE)
    db_session.add(expense)
    db_session.commit()
    db_session.add(
        BankRule(
            name="Coffee",
            pattern="coffee",
            rule_type="contains",
            account_id=expense.id,
        )
    )
    db_session.commit()

    import_csv_transactions(db_session, ba.id, CHASE_CHECKING_CSV)
    coffee = (
        db_session.query(BankTransaction)
        .filter(BankTransaction.payee == "COFFEE SHOP")
        .all()
    )
    assert all(t.match_status == "unmatched" for t in coffee)  # categorised, not posted
    assert all(t.category_account_id == expense.id for t in coffee)


def test_unknown_format_reports_error(db_session):
    ba = _mk_bank_account(db_session)
    result = import_csv_transactions(db_session, ba.id, "Foo,Bar\n1,2\n")
    assert result["imported"] == 0
    assert result["errors"]
