"""Reconciliation over the ledger's lines (issue #114)."""

from datetime import date
from decimal import Decimal

import pytest

from app.models.banking import BankTransaction
from app.models.transactions import TransactionLine
from app.services.ofx_import import import_transactions


@pytest.fixture
def vendor(client):
    return client.post("/api/vendors", json={"name": "Grid Power"}).json()


def _expense(client, seed_accounts, vendor, amount, day, paid_from="1000"):
    r = client.post(
        "/api/expenses",
        json={
            "date": f"2026-09-{day:02d}",
            "vendor_id": vendor["id"],
            "expense_account_id": seed_accounts["6000"].id,
            "paid_from_account_id": seed_accounts[paid_from].id,
            "amount": amount,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _deposit(client, seed_accounts, amount, day, account="1000"):
    r = client.post(
        "/api/banking/transactions",
        json={
            "account_id": seed_accounts[account].id,
            "date": f"2026-09-{day:02d}",
            "amount": amount,
            "category_account_id": seed_accounts["4000"].id,
            "payee": "walk-in",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _start(client, seed_accounts, balance, day=30, account="1000"):
    return client.post(
        "/api/banking/reconciliations",
        json={
            "account_id": seed_accounts[account].id,
            "statement_date": f"2026-09-{day:02d}",
            "statement_balance": balance,
        },
    )


def test_start_session_toggle_complete_and_the_next_statement(
    client, db_session, seed_accounts, vendor
):
    _deposit(client, seed_accounts, "1000", 1)
    e = _expense(client, seed_accounts, vendor, "150", 5)
    _expense(client, seed_accounts, vendor, "40", 6)  # not on the statement yet
    late = _deposit(
        client, seed_accounts, "20", 30
    )  # after nothing, on the date; a later one below
    _deposit(client, seed_accounts, "999", 30)
    r = _start(client, seed_accounts, "850.00", day=29)
    assert r.status_code == 201, r.text
    recon = r.json()
    assert (
        recon["beginning_balance"] == "0.00"
        and recon["account_id"] == seed_accounts["1000"].id
    )
    assert _start(client, seed_accounts, "1").status_code == 409  # one open per account
    s = client.get(f"/api/banking/reconciliations/{recon['id']}/transactions").json()
    assert s["beginning_balance"] == 0.0 and s["statement_balance"] == 850.0
    assert [x["amount"] for x in s["transactions"]] == [
        1000.0,
        -150.0,
        -40.0,
    ]  # lines after the 29th are absent
    assert s["difference"] == 850.0 and all(
        x["reconciled"] is False for x in s["transactions"]
    )
    by_txn = {x["transaction_id"]: x["id"] for x in s["transactions"]}
    dep_line = next(x["id"] for x in s["transactions"] if x["amount"] == 1000.0)
    for line_id in (dep_line, by_txn[e["id"]]):
        r = client.post(f"/api/banking/reconciliations/{recon['id']}/toggle/{line_id}")
        assert r.status_code == 200 and r.json()["reconciled"] is True
    s = client.get(f"/api/banking/reconciliations/{recon['id']}/transactions").json()
    assert (
        s["cleared_total"] == 850.0
        and s["difference"] == 0.0
        and s["uncleared_total"] == -40.0
    )
    # guards: a line after the statement date, a line on another account
    late_line = (
        db_session.query(TransactionLine)
        .filter(
            TransactionLine.transaction_id == late["id"],
            TransactionLine.account_id == seed_accounts["1000"].id,
        )
        .one()
    )
    assert (
        client.post(
            f"/api/banking/reconciliations/{recon['id']}/toggle/{late_line.id}"
        ).status_code
        == 400
    )
    other = (
        db_session.query(TransactionLine)
        .filter(TransactionLine.account_id == seed_accounts["6000"].id)
        .first()
    )
    assert (
        client.post(
            f"/api/banking/reconciliations/{recon['id']}/toggle/{other.id}"
        ).status_code
        == 400
    )
    r = client.post(f"/api/banking/reconciliations/{recon['id']}/complete")
    assert r.status_code == 200 and r.json()["cleared_count"] == 2, r.text
    assert (
        client.post(
            f"/api/banking/reconciliations/{recon['id']}/toggle/{dep_line}"
        ).status_code
        == 400
    )
    # the reconciled expense cannot be voided; the unreconciled one can
    assert client.post(f"/api/expenses/{e['id']}/void").status_code == 400
    # next statement starts from 850 and only sees what is still open
    r = _start(client, seed_accounts, "1829.00", day=30)
    assert r.status_code == 201 and r.json()["beginning_balance"] == "850.00"
    s = client.get(f"/api/banking/reconciliations/{r.json()['id']}/transactions").json()
    assert sorted(x["amount"] for x in s["transactions"]) == [-40.0, 20.0, 999.0]
    for x in s["transactions"]:
        client.post(f"/api/banking/reconciliations/{r.json()['id']}/toggle/{x['id']}")
    assert (
        client.post(
            f"/api/banking/reconciliations/{r.json()['id']}/complete"
        ).status_code
        == 200
    )
    hist = client.get(
        f"/api/banking/reconciliations?account_id={seed_accounts['1000'].id}"
    ).json()
    assert [h["cleared_total"] for h in hist] == ["979.00", "850.00"]
    assert (
        client.get("/api/banking/overview").json()[0]["last_reconciled"] == "2026-09-30"
    )


def test_difference_must_be_zero_and_abandon_keeps_ticks(
    client, db_session, seed_accounts, vendor
):
    e = _expense(client, seed_accounts, vendor, "10", 1)
    recon = _start(client, seed_accounts, "-10").json()
    assert (
        client.post(f"/api/banking/reconciliations/{recon['id']}/complete").status_code
        == 400
    )
    line = (
        db_session.query(TransactionLine)
        .filter(
            TransactionLine.transaction_id == e["id"],
            TransactionLine.account_id == seed_accounts["1000"].id,
        )
        .one()
    )
    client.post(f"/api/banking/reconciliations/{recon['id']}/toggle/{line.id}")
    r = client.delete(f"/api/banking/reconciliations/{recon['id']}")
    assert r.status_code == 200
    db_session.expire_all()
    assert line.cleared is True and line.reconciliation_id is None
    assert (
        client.get(
            f"/api/banking/reconciliations?account_id={seed_accounts['1000'].id}"
        ).json()
        == []
    )
    # a fresh session sees the tick and completes at once
    recon = _start(client, seed_accounts, "-10").json()
    assert (
        client.post(f"/api/banking/reconciliations/{recon['id']}/complete").status_code
        == 200
    )


def test_matched_statement_lines_arrive_cleared_and_a_card_reconciles_to_what_is_owed(
    client, db_session, seed_accounts, vendor
):
    feed = client.post(
        "/api/banking/accounts",
        json={"name": "Visa", "account_id": seed_accounts["2100"].id},
    ).json()
    e = _expense(client, seed_accounts, vendor, "80", 3, paid_from="2100")
    out = import_transactions(
        db_session,
        feed["id"],
        [
            {
                "fitid": "c1",
                "date": date(2026, 9, 3),
                "amount": Decimal("-80"),
                "payee": "GRID POWER",
                "memo": "",
            }
        ],
    )
    assert out["matched"] == 1
    recon = _start(client, seed_accounts, "80", account="2100").json()
    s = client.get(f"/api/banking/reconciliations/{recon['id']}/transactions").json()
    [row] = s["transactions"]
    assert (
        row["reconciled"] is True and row["matched"] is True and row["amount"] == 80.0
    )
    assert s["difference"] == 0.0
    assert (
        client.post(f"/api/banking/reconciliations/{recon['id']}/complete").status_code
        == 200
    )
    bt = db_session.query(BankTransaction).one()
    assert (
        client.post(f"/api/banking/transactions/{bt.id}/unmatch").status_code == 400
    )  # closed month
    assert client.post(f"/api/expenses/{e['id']}/void").status_code == 400


def test_not_a_bank_account(client, seed_accounts):
    assert _start(client, seed_accounts, "0", account="1100").status_code == 400
