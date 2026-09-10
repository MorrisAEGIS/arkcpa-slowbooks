"""Dashboard bank balances come from the ledger, and cards are not cash."""


def test_bank_balances_and_cash_position_from_the_ledger(
    client, db_session, seed_accounts
):
    from app.services.dashboard_widgets import bank_balances, cash_position

    r = client.post(
        "/api/journal",
        json={
            "date": "2026-09-01",
            "description": "cash in",
            "lines": [
                {"account_id": seed_accounts["1000"].id, "debit": "700", "credit": "0"},
                {"account_id": seed_accounts["3000"].id, "debit": "0", "credit": "700"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/api/cc-charges",
        json={
            "date": "2026-09-01",
            "payee": "Fuel",
            "amount": "40",
            "account_id": seed_accounts["6000"].id,
        },
    )
    assert r.status_code in (200, 201), r.text
    bb = bank_balances(db_session)
    by = {a["name"]: a for a in bb["accounts"]}
    assert by["Checking"]["balance"] == 700.0 and by["Checking"]["kind"] == "bank"
    assert (
        by["Credit Card"]["balance"] == 40.0
        and by["Credit Card"]["kind"] == "credit_card"
    )
    assert bb["total"] == 700.0  # the card is owed, not cash
    assert cash_position(db_session)["cash"] == 700.0
