"""Credit-card charges: any card (default 2100), status, void."""

from decimal import Decimal

from app.models.transactions import Transaction


def _charge(client, seed_accounts, **over):
    body = {
        "date": "2026-09-01",
        "payee": "Fuel",
        "amount": "40",
        "account_id": seed_accounts["6000"].id,
    }
    body.update(over)
    return client.post("/api/cc-charges", json=body)


def test_default_card_is_2100_and_a_named_card_works(client, db_session, seed_accounts):
    r = _charge(client, seed_accounts)
    assert r.status_code == 201, r.text
    txn = db_session.query(Transaction).get(r.json()["transaction_id"])
    assert {ln.account_id for ln in txn.lines if ln.credit > 0} == {
        seed_accounts["2100"].id
    }
    visa = client.post(
        "/api/accounts",
        json={
            "name": "Chase Visa",
            "account_number": "2150",
            "account_type": "liability",
            "bank_kind": "credit_card",
        },
    ).json()
    r = _charge(client, seed_accounts, card_account_id=visa["id"], amount="12.50")
    assert r.status_code == 201, r.text
    txn = db_session.query(Transaction).get(r.json()["transaction_id"])
    assert {ln.account_id: ln.credit for ln in txn.lines if ln.credit > 0} == {
        visa["id"]: Decimal("12.50")
    }
    rows = {x["id"]: x for x in client.get("/api/cc-charges").json()}
    assert (
        rows[txn.id]["card_account_name"] == "Chase Visa"
        and rows[txn.id]["status"] == "recorded"
    )
    assert (
        _charge(
            client, seed_accounts, card_account_id=seed_accounts["1000"].id
        ).status_code
        == 400
    )


def test_void_reverses_and_reports_status(client, seed_accounts):
    cid = _charge(client, seed_accounts).json()["transaction_id"]
    r = client.post(f"/api/cc-charges/{cid}/void")
    assert r.status_code == 200 and r.json()["status"] == "void", r.text
    assert client.post(f"/api/cc-charges/{cid}/void").status_code == 400
    assert {x["id"]: x["status"] for x in client.get("/api/cc-charges").json()}[
        cid
    ] == "void"
    assert (
        client.get(
            f"/api/banking/check-register?account_id={seed_accounts['2100'].id}"
        ).json()["balance"]
        == 0.0
    )
