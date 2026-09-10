"""A register entry posts to the ledger (issue #114)."""

from decimal import Decimal

from app.models.banking import BankTransaction
from app.models.transactions import Transaction


def _lines(db, txn_id):
    txn = db.query(Transaction).filter(Transaction.id == txn_id).one()
    dr = {ln.account_id: ln.debit for ln in txn.lines if ln.debit > 0}
    cr = {ln.account_id: ln.credit for ln in txn.lines if ln.credit > 0}
    return txn, dr, cr


def _entry(client, seed_accounts, account="1000", **over):
    body = {
        "account_id": seed_accounts[account].id,
        "date": "2026-09-02",
        "amount": "-125.00",
        "category_account_id": seed_accounts["6000"].id,
        "payee": "City Water",
        "description": "August water",
        "check_number": "1042",
    }
    body.update(over)
    return client.post("/api/banking/transactions", json=body)


def test_money_out_debits_the_category_and_credits_the_bank(
    client, db_session, seed_accounts
):
    r = _entry(client, seed_accounts)
    assert r.status_code == 201, r.text
    body = r.json()
    txn, dr, cr = _lines(db_session, body["id"])
    assert txn.source_type == "bank_entry" and txn.reference == "1042"
    assert dr == {seed_accounts["6000"].id: Decimal("125.00")}
    assert cr == {seed_accounts["1000"].id: Decimal("125.00")}
    assert body["amount"] == "-125.00" and body["category_name"]
    assert body["payee"] == "City Water" and body["status"] == "recorded"
    reg = client.get(
        f"/api/banking/check-register?account_id={seed_accounts['1000'].id}"
    ).json()
    [row] = reg["entries"]
    assert (
        row["payment"] == 125.0
        and row["reference"] == "1042"
        and row["voidable"] is True
    )
    assert row["payee"] == "City Water"
    assert db_session.query(BankTransaction).count() == 0  # no side-ledger row


def test_money_in_debits_the_bank(client, db_session, seed_accounts):
    r = _entry(
        client,
        seed_accounts,
        amount="300",
        category_account_id=seed_accounts["4000"].id,
        payee="Walk-in",
    )
    assert r.status_code == 201, r.text
    _, dr, cr = _lines(db_session, r.json()["id"])
    assert dr == {seed_accounts["1000"].id: Decimal("300.00")}
    assert cr == {seed_accounts["4000"].id: Decimal("300.00")}


def test_card_charge_and_card_payment(client, db_session, seed_accounts):
    r = _entry(client, seed_accounts, account="2100", amount="-50", payee="Fuel")
    assert r.status_code == 201, r.text
    _, dr, cr = _lines(db_session, r.json()["id"])
    assert dr == {seed_accounts["6000"].id: Decimal("50.00")} and cr == {
        seed_accounts["2100"].id: Decimal("50.00")
    }
    # paying the card from checking is a transfer
    r = _entry(
        client,
        seed_accounts,
        account="2100",
        amount="500",
        category_account_id=seed_accounts["1000"].id,
        payee="Payment",
    )
    assert r.status_code == 201, r.text
    txn, dr, cr = _lines(db_session, r.json()["id"])
    assert txn.source_type == "transfer"
    assert dr == {seed_accounts["2100"].id: Decimal("500.00")} and cr == {
        seed_accounts["1000"].id: Decimal("500.00")
    }
    reg = client.get(
        f"/api/banking/check-register?account_id={seed_accounts['2100'].id}"
    ).json()
    assert [x["balance"] for x in reg["entries"]] == [50.0, -450.0]


def test_validation(client, seed_accounts):
    assert _entry(client, seed_accounts, amount="0").status_code == 400
    del_body = {
        "account_id": seed_accounts["1000"].id,
        "date": "2026-09-02",
        "amount": "-1",
    }
    assert (
        client.post("/api/banking/transactions", json=del_body).status_code == 422
    )  # category required
    assert (
        _entry(
            client, seed_accounts, category_account_id=seed_accounts["1000"].id
        ).status_code
        == 400
    )
    assert (
        _entry(client, seed_accounts, account="1100").status_code == 400
    )  # not a bank account
    assert (
        client.post(
            "/api/banking/transactions",
            json={
                "date": "2026-09-02",
                "amount": "-1",
                "category_account_id": seed_accounts["6000"].id,
            },
        ).status_code
        == 422
    )


def test_legacy_bank_account_id_resolves_to_the_ledger_account(
    client, db_session, seed_accounts
):
    feed = client.post(
        "/api/banking/accounts",
        json={"name": "feed", "account_id": seed_accounts["1000"].id},
    ).json()
    body = {
        "bank_account_id": feed["id"],
        "date": "2026-09-02",
        "amount": "-5",
        "category_account_id": seed_accounts["6000"].id,
    }
    r = client.post("/api/banking/transactions", json=body)
    assert r.status_code == 201 and r.json()["account_id"] == seed_accounts["1000"].id


def test_closing_date_blocks_a_backdated_entry(client, seed_accounts):
    assert (
        client.put("/api/settings", json={"closing_date": "2026-12-31"}).status_code
        == 200
    )
    assert _entry(client, seed_accounts, date="2026-06-15").status_code == 403


def test_void_posts_a_reversal_and_shows_both_rows(client, db_session, seed_accounts):
    txn_id = _entry(client, seed_accounts).json()["id"]
    r = client.post(f"/api/banking/entries/{txn_id}/void")
    assert r.status_code == 200 and r.json()["status"] == "void", r.text
    assert client.post(f"/api/banking/entries/{txn_id}/void").status_code == 400
    reg = client.get(
        f"/api/banking/check-register?account_id={seed_accounts['1000'].id}"
    ).json()
    assert [(x["source_type"], x["voided"], x["voidable"]) for x in reg["entries"]] == [
        ("bank_entry", True, False),
        ("bank_entry_void", True, False),
    ]
    assert reg["balance"] == 0.0
    total = db_session.query(Transaction).count()
    assert total == 2
