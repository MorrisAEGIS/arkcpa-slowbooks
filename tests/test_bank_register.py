"""The bank register is the ledger account (issue #114): GL rows, natural
running balance, payees and links, and balances from the ledger."""

from decimal import Decimal

import pytest


@pytest.fixture
def vendor(client):
    r = client.post("/api/vendors", json={"name": "Sweet Forest Cafe"})
    assert r.status_code == 201, r.text
    return r.json()


def _expense(
    client, seed_accounts, vendor, amount="30.30", paid_from="1000", date="2026-09-02"
):
    r = client.post(
        "/api/expenses",
        json={
            "date": date,
            "vendor_id": vendor["id"],
            "expense_account_id": seed_accounts["6000"].id,
            "paid_from_account_id": seed_accounts[paid_from].id,
            "amount": amount,
            "reference": "rcpt 1187",
            "memo": "team lunch",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_register_is_the_ledger_account_with_payees_and_links(
    client, seed_accounts, vendor
):
    e = _expense(client, seed_accounts, vendor)
    r = client.post(
        "/api/journal",
        json={
            "date": "2026-09-03",
            "description": "owner puts cash in",
            "lines": [
                {"account_id": seed_accounts["1000"].id, "debit": "500", "credit": "0"},
                {"account_id": seed_accounts["3000"].id, "debit": "0", "credit": "500"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    reg = client.get(
        f"/api/banking/check-register?account_id={seed_accounts['1000'].id}"
    ).json()
    assert reg["bank_kind"] == "bank" and reg["natural_balance"] == "debit"
    rows = reg["entries"]
    assert [(x["date"], x["payment"], x["deposit"], x["balance"]) for x in rows] == [
        ("2026-09-02", 30.3, 0, -30.3),
        ("2026-09-03", 0, 500.0, 469.7),
    ]
    assert rows[0]["payee"] == "Sweet Forest Cafe"
    assert (
        rows[0]["source_type"] == "expense"
        and rows[0]["source_link"] == f"/#/expenses/{e['id']}"
    )
    assert (
        rows[0]["cleared"] is False
        and rows[0]["voided"] is False
        and rows[0]["voidable"] is False
    )
    assert rows[1]["source_link"].startswith("/#/journal/")
    assert reg["balance"] == 469.7
    # no register row of the old kind is involved anywhere
    assert client.get("/api/banking/transactions").json() == []


def test_card_register_shows_the_amount_owed_positive(client, seed_accounts, vendor):
    _expense(client, seed_accounts, vendor, amount="80", paid_from="2100")
    reg = client.get(
        f"/api/banking/check-register?account_id={seed_accounts['2100'].id}"
    ).json()
    assert reg["bank_kind"] == "credit_card" and reg["natural_balance"] == "credit"
    [row] = reg["entries"]
    assert row["payment"] == 80.0 and row["deposit"] == 0 and row["balance"] == 80.0
    assert reg["balance"] == 80.0


def test_register_defaults_to_the_first_bank_account_and_refuses_nothing_else(
    client, seed_accounts
):
    assert (
        client.get("/api/banking/check-register").json()["account_id"]
        == seed_accounts["1000"].id
    )
    r = client.get(f"/api/banking/check-register?account_id={seed_accounts['1100'].id}")
    assert (
        r.status_code == 200 and r.json()["bank_kind"] is None
    )  # any account can be viewed


def test_date_range_carries_an_opening_balance(client, seed_accounts, vendor):
    _expense(client, seed_accounts, vendor, amount="10", date="2026-01-05")
    _expense(client, seed_accounts, vendor, amount="20", date="2026-02-05")
    reg = client.get(
        f"/api/banking/check-register?account_id={seed_accounts['1000'].id}&start_date=2026-02-01&end_date=2026-02-28"
    ).json()
    assert reg["opening_balance"] == -10.0
    assert [x["balance"] for x in reg["entries"]] == [-30.0]
    drill = client.get(
        f"/api/reports/account-transactions?account_id={seed_accounts['1000'].id}&start_date=2026-01-01&end_date=2026-12-31"
    ).json()
    assert drill["period_credit"] == 30.0 and len(drill["entries"]) == 2
    assert {k for k in drill["entries"][0]} >= {
        "transaction_id",
        "running_balance",
        "source_link",
        "cleared",
        "payee",
    }


def test_feed_carries_the_ledger_balance_and_an_opening_balance_posts(
    client, seed_accounts
):
    r = client.post(
        "/api/banking/accounts",
        json={
            "name": "Numerica Operating",
            "account_id": seed_accounts["1000"].id,
            "bank_name": "Numerica",
            "last_four": "8841",
            "opening_balance": "42500.00",
            "opening_date": "2026-01-01",
        },
    )
    assert r.status_code == 201, r.text
    feed = r.json()
    assert feed["bank_kind"] == "bank" and Decimal(feed["balance"]) == Decimal(
        "42500.00"
    )
    assert feed["legacy_balance"] is None
    reg = client.get(
        f"/api/banking/check-register?account_id={seed_accounts['1000'].id}"
    ).json()
    [row] = reg["entries"]
    assert row["source_type"] == "opening_balance" and row["deposit"] == 42500.0
    obe = next(
        a for a in client.get("/api/accounts").json() if a["account_number"] == "3900"
    )
    assert obe["name"] == "Opening Balance Equity" and obe["account_type"] == "equity"
    # a second feed on the same ledger account is refused; the old field is gone
    r = client.post(
        "/api/banking/accounts",
        json={"name": "dup", "account_id": seed_accounts["1000"].id},
    )
    assert r.status_code == 409
    r = client.post(
        "/api/banking/accounts",
        json={"name": "x", "account_id": seed_accounts["1000"].id, "balance": "1"},
    )
    assert r.status_code == 422
    r = client.post(
        "/api/banking/accounts",
        json={"name": "ar", "account_id": seed_accounts["1100"].id},
    )
    assert r.status_code == 400 and "not a bank" in r.json()["detail"]


def test_card_opening_balance_is_what_is_owed(client, seed_accounts):
    r = client.post(
        "/api/banking/accounts",
        json={
            "name": "Visa",
            "account_id": seed_accounts["2100"].id,
            "opening_balance": "4500",
            "opening_date": "2026-01-01",
        },
    )
    assert r.status_code == 201, r.text
    assert Decimal(r.json()["balance"]) == Decimal("4500")
    reg = client.get(
        f"/api/banking/check-register?account_id={seed_accounts['2100'].id}"
    ).json()
    assert reg["entries"][0]["payment"] == 4500.0 and reg["balance"] == 4500.0
    bs = client.get("/api/reports/balance-sheet").json()
    assert (
        abs(bs["total_assets"] - (bs["total_liabilities"] + bs["total_equity"])) < 0.01
    )


def test_overview_lists_bank_and_card_accounts_from_the_ledger(
    client, seed_accounts, vendor
):
    _expense(client, seed_accounts, vendor, amount="25")
    client.post(
        "/api/banking/accounts",
        json={"name": "Checking feed", "account_id": seed_accounts["1000"].id},
    )
    rows = {r["account_number"]: r for r in client.get("/api/banking/overview").json()}
    assert set(rows) == {"1000", "1010", "2100"}
    assert (
        rows["1000"]["balance"] == -25.0
        and rows["1000"]["feed"]["name"] == "Checking feed"
    )
    assert rows["1000"]["to_review"] == 0 and rows["1000"]["last_reconciled"] is None
    assert rows["1010"]["feed"] is None and rows["2100"]["bank_kind"] == "credit_card"


def test_legacy_balance_can_be_posted_once_or_dismissed(
    client, db_session, seed_accounts
):
    from app.models.banking import BankAccount

    ba = BankAccount(
        name="Old feed",
        account_id=seed_accounts["1000"].id,
        legacy_balance=Decimal("123.45"),
    )
    db_session.add(ba)
    db_session.commit()
    feed = client.get(f"/api/banking/accounts/{ba.id}").json()
    assert (
        Decimal(feed["legacy_balance"]) == Decimal("123.45")
        and Decimal(feed["balance"]) == 0
    )
    r = client.post(
        f"/api/banking/accounts/{ba.id}/post-legacy-balance",
        json={"date": "2026-01-01"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["legacy_balance"] is None and Decimal(
        r.json()["balance"]
    ) == Decimal("123.45")
    assert (
        client.post(
            f"/api/banking/accounts/{ba.id}/post-legacy-balance",
            json={"date": "2026-01-01"},
        ).status_code
        == 400
    )
    # dismiss on another feed
    ba2 = BankAccount(
        name="Old card",
        account_id=seed_accounts["2100"].id,
        legacy_balance=Decimal("-9"),
    )
    db_session.add(ba2)
    db_session.commit()
    r = client.put(f"/api/banking/accounts/{ba2.id}", json={"legacy_balance": None})
    assert r.status_code == 200 and r.json()["legacy_balance"] is None
    assert (
        client.put(
            f"/api/banking/accounts/{ba2.id}", json={"legacy_balance": "5"}
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/banking/check-register?account_id={seed_accounts['2100'].id}"
        ).json()["entries"]
        == []
    )
