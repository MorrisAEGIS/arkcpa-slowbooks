"""Everything the banking work posts keeps the books in balance."""

from datetime import date
from decimal import Decimal

from sqlalchemy import func

from app.models.transactions import Transaction, TransactionLine
from app.services.ofx_import import import_transactions


def test_banking_scenario_keeps_every_invariant(client, db_session, seed_accounts):
    vendor = client.post("/api/vendors", json={"name": "Grid Power"}).json()
    feed = client.post(
        "/api/banking/accounts",
        json={
            "name": "Checking",
            "account_id": seed_accounts["1000"].id,
            "opening_balance": "5000",
            "opening_date": "2026-01-01",
        },
    ).json()
    card = client.post(
        "/api/banking/accounts",
        json={
            "name": "Visa",
            "account_id": seed_accounts["2100"].id,
            "opening_balance": "300",
            "opening_date": "2026-01-01",
        },
    ).json()
    # register entries, a transfer, a card charge, an expense
    client.post(
        "/api/banking/transactions",
        json={
            "account_id": seed_accounts["1000"].id,
            "date": "2026-02-01",
            "amount": "-120",
            "category_account_id": seed_accounts["6000"].id,
            "payee": "rent",
        },
    )
    client.post(
        "/api/banking/transactions",
        json={
            "account_id": seed_accounts["1000"].id,
            "date": "2026-02-02",
            "amount": "700",
            "category_account_id": seed_accounts["4000"].id,
            "payee": "sale",
        },
    )
    t = client.post(
        "/api/transfers",
        json={
            "date": "2026-02-03",
            "from_account_id": seed_accounts["1000"].id,
            "to_account_id": seed_accounts["2100"].id,
            "amount": "300",
        },
    ).json()
    c = client.post(
        "/api/cc-charges",
        json={
            "date": "2026-02-04",
            "payee": "fuel",
            "amount": "45",
            "account_id": seed_accounts["6000"].id,
        },
    ).json()
    e = client.post(
        "/api/expenses",
        json={
            "date": "2026-02-05",
            "vendor_id": vendor["id"],
            "expense_account_id": seed_accounts["6000"].id,
            "paid_from_account_id": seed_accounts["1000"].id,
            "amount": "60",
        },
    ).json()
    # feed: one matches the expense, one is added, one is a card payment
    out = import_transactions(
        db_session,
        feed["id"],
        [
            {
                "fitid": "a",
                "date": date(2026, 2, 5),
                "amount": Decimal("-60"),
                "payee": "GRID POWER",
                "memo": "",
            },
            {
                "fitid": "b",
                "date": date(2026, 2, 6),
                "amount": Decimal("-33"),
                "payee": "PARKING",
                "memo": "",
            },
        ],
    )
    assert out["matched"] == 1
    rows = client.get(
        f"/api/banking/transactions?bank_account_id={feed['id']}&status=unmatched"
    ).json()
    assert (
        client.post(
            f"/api/banking/transactions/{rows[0]['id']}/add",
            json={"category_account_id": seed_accounts["6000"].id},
        ).status_code
        == 200
    )
    import_transactions(
        db_session,
        card["id"],
        [
            {
                "fitid": "p",
                "date": date(2026, 2, 3),
                "amount": Decimal("300"),
                "payee": "PAYMENT",
                "memo": "",
            }
        ],
    )
    # two voids
    assert client.post(f"/api/transfers/{t['id']}/void").status_code == 200
    assert client.post(f"/api/cc-charges/{c['transaction_id']}/void").status_code == 200

    # every journal entry balances, and the ledger as a whole
    for txn in db_session.query(Transaction).all():
        dr = sum((ln.debit for ln in txn.lines), Decimal("0"))
        cr = sum((ln.credit for ln in txn.lines), Decimal("0"))
        assert dr == cr, (txn.id, txn.source_type, dr, cr)
    tot = db_session.query(
        func.sum(TransactionLine.debit), func.sum(TransactionLine.credit)
    ).one()
    assert Decimal(str(tot[0])) == Decimal(str(tot[1]))
    bs = client.get("/api/reports/balance-sheet").json()
    assert (
        abs(bs["total_assets"] - (bs["total_liabilities"] + bs["total_equity"])) < 0.01
    )
    tb = client.get("/api/reports/trial-balance").json()
    debits = tb.get("total_debits", tb.get("total_debit"))
    credits = tb.get("total_credits", tb.get("total_credit"))
    assert debits is not None and abs(debits - credits) < 0.01
    # the register's balance is the ledger's balance
    reg = client.get(
        f"/api/banking/check-register?account_id={seed_accounts['1000'].id}"
    ).json()
    assert reg["balance"] == 5000 - 120 + 700 - 300 - 60 - 33 + 300  # transfer voided
    assert reg["balance"] == client.get("/api/banking/overview").json()[0]["balance"]
    assert e["id"]
