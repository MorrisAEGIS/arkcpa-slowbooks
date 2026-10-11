"""Multi-currency: home-currency GL conversion at booked rates,
cross-currency payment validation, and realized FX gain/loss."""

from decimal import Decimal

import pytest

from app.models.accounts import Account
from app.models.transactions import Transaction, TransactionLine
from app.services.currency import convert_lines

# ── convert_lines ────────────────────────────────────────────────────────


def test_convert_lines_balances_after_rounding():
    lines = [
        {"account_id": 1, "debit": Decimal("33.33"), "credit": Decimal("0")},
        {"account_id": 2, "debit": Decimal("33.33"), "credit": Decimal("0")},
        {"account_id": 3, "debit": Decimal("0"), "credit": Decimal("66.66")},
    ]
    converted = convert_lines(lines, Decimal("1.13579"))
    debit = sum(ln["debit"] for ln in converted)
    credit = sum(ln["credit"] for ln in converted)
    assert debit == credit


def test_convert_lines_rate_one_is_identity():
    lines = [{"account_id": 1, "debit": Decimal("5"), "credit": Decimal("0")}]
    assert convert_lines(lines, Decimal("1")) is lines


# ── Foreign invoice posts home-currency GL ───────────────────────────────


@pytest.fixture
def eur_invoice(client, seed_accounts, seed_customer):
    resp = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-07-01",
            "currency": "EUR",
            "exchange_rate": "1.10",
            "lines": [{"description": "consulting", "quantity": 1, "rate": 100}],
        },
    )
    assert resp.status_code in (200, 201), resp.text
    issued = client.post(f"/api/invoices/{resp.json()['id']}/issue")
    assert issued.status_code == 200, issued.text
    return issued.json()


def test_foreign_invoice_journal_in_home_currency(eur_invoice, db_session):
    inv = eur_invoice
    assert inv["currency"] == "EUR"
    assert Decimal(str(inv["total"])) == Decimal("100.00")  # document currency
    txn = (
        db_session.query(Transaction)
        .filter(
            Transaction.source_type == "invoice", Transaction.source_id == inv["id"]
        )
        .one()
    )
    lines = db_session.query(TransactionLine).filter_by(transaction_id=txn.id).all()
    debit = sum(ln.debit or 0 for ln in lines)
    credit = sum(ln.credit or 0 for ln in lines)
    # GL carries 100 EUR * 1.10 = 110.00 home
    assert debit == credit == Decimal("110.00")


def test_home_invoice_defaults(client, seed_accounts, seed_customer):
    resp = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-07-01",
            "lines": [{"description": "x", "quantity": 1, "rate": 50}],
        },
    )
    inv = resp.json()
    assert inv["currency"] == "USD"
    assert Decimal(str(inv["exchange_rate"])) == Decimal("1")


def test_foreign_invoice_without_rate_and_feed_fails(
    client, seed_accounts, seed_customer, monkeypatch
):
    import app.services.fx_service as fx

    monkeypatch.setattr(
        fx,
        "get_rate",
        lambda f, t: {
            "rate": None,
            "observation_date": None,
            "source": None,
            "error": "down",
        },
    )
    resp = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-07-01",
            "currency": "EUR",
            "lines": [{"description": "x", "quantity": 1, "rate": 10}],
        },
    )
    assert resp.status_code == 400
    assert "exchange_rate" in resp.json()["detail"]


# ── Payments: validation + realized FX ───────────────────────────────────


def test_cross_currency_payment_rejected(client, eur_invoice, seed_customer):
    resp = client.post(
        "/api/payments",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-07-05",
            "amount": 100,
            "currency": "USD",
            "allocations": [{"invoice_id": eur_invoice["id"], "amount": 100}],
        },
    )
    assert resp.status_code == 400
    assert "does not match" in resp.json()["detail"]


def test_payment_realizes_fx_gain(client, db_session, eur_invoice, seed_customer):
    """Invoice booked at 1.10; paid at 1.20 → cash 120 home vs A/R 110
    relieved → 10.00 realized gain (credit on the FX account)."""
    resp = client.post(
        "/api/payments",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-07-06",
            "amount": 100,
            "currency": "EUR",
            "exchange_rate": "1.20",
            "allocations": [{"invoice_id": eur_invoice["id"], "amount": 100}],
        },
    )
    assert resp.status_code in (200, 201), resp.text

    fx_acct = db_session.query(Account).filter(Account.account_number == "6999").one()
    txn = (
        db_session.query(Transaction)
        .filter(Transaction.source_type == "payment")
        .order_by(Transaction.id.desc())
        .first()
    )
    lines = db_session.query(TransactionLine).filter_by(transaction_id=txn.id).all()
    assert sum(ln.debit or 0 for ln in lines) == sum(ln.credit or 0 for ln in lines)
    fx_lines = [ln for ln in lines if ln.account_id == fx_acct.id]
    assert len(fx_lines) == 1
    assert fx_lines[0].credit == Decimal("10.00")  # gain

    # invoice settles in document currency
    inv = client.get(f"/api/invoices/{eur_invoice['id']}").json()
    assert inv["status"] == "paid"
    assert Decimal(str(inv["balance_due"])) == Decimal("0.00")


def test_payment_realizes_fx_loss(client, db_session, eur_invoice, seed_customer):
    resp = client.post(
        "/api/payments",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-07-06",
            "amount": 100,
            "currency": "EUR",
            "exchange_rate": "1.05",
            "allocations": [{"invoice_id": eur_invoice["id"], "amount": 100}],
        },
    )
    assert resp.status_code in (200, 201), resp.text
    fx_acct = db_session.query(Account).filter(Account.account_number == "6999").one()
    txn = (
        db_session.query(Transaction)
        .filter(Transaction.source_type == "payment")
        .order_by(Transaction.id.desc())
        .first()
    )
    fx_lines = [
        ln
        for ln in db_session.query(TransactionLine).filter_by(transaction_id=txn.id)
        if ln.account_id == fx_acct.id
    ]
    assert fx_lines[0].debit == Decimal("5.00")  # loss


def test_online_checkout_blocked_for_foreign_invoice(
    unauthed_client, client, db_session, eur_invoice
):
    from app.services.settings_service import set_setting

    set_setting(db_session, "stripe_enabled", "true")
    set_setting(db_session, "stripe_secret_key", "sk_test_x")
    db_session.commit()
    inv = client.get(f"/api/invoices/{eur_invoice['id']}").json()
    resp = unauthed_client.post(
        "/api/payments/stripe/create-checkout-session",
        json={"payment_token": inv["payment_token"]},
    )
    assert resp.status_code == 400
    assert "home-currency" in resp.json()["detail"]


# ── Foreign bill ─────────────────────────────────────────────────────────


def test_foreign_bill_posts_home_gl(client, db_session, seed_accounts):
    from app.models.contacts import Vendor

    vendor = Vendor(name="Euro Supplier", is_active=True)
    db_session.add(vendor)
    db_session.commit()

    expense = (
        db_session.query(Account).filter(Account.name == "Office Supplies").first()
    ) or db_session.query(Account).first()
    resp = client.post(
        "/api/bills",
        json={
            "vendor_id": vendor.id,
            "bill_number": "EUR-B1",
            "date": "2026-07-02",
            "currency": "EUR",
            "exchange_rate": "1.10",
            "lines": [
                {
                    "account_id": expense.id,
                    "description": "supplies",
                    "quantity": 1,
                    "rate": 200,
                }
            ],
        },
    )
    assert resp.status_code in (200, 201), resp.text
    bill = resp.json()
    assert bill["currency"] == "EUR"
    txn = (
        db_session.query(Transaction)
        .filter(Transaction.source_type == "bill", Transaction.source_id == bill["id"])
        .one()
    )
    lines = db_session.query(TransactionLine).filter_by(transaction_id=txn.id).all()
    assert sum(ln.debit or 0 for ln in lines) == Decimal("220.00")


# ── A/P realized FX (bill payments) ──────────────────────────────────────


@pytest.fixture
def eur_bill(client, db_session, seed_accounts):
    from app.models.contacts import Vendor

    vendor = Vendor(name="Euro AP Supplier", is_active=True)
    db_session.add(vendor)
    db_session.commit()
    expense = (
        db_session.query(Account).filter(Account.name == "Office Supplies").first()
    ) or db_session.query(Account).first()
    resp = client.post(
        "/api/bills",
        json={
            "vendor_id": vendor.id,
            "bill_number": "EUR-AP-1",
            "date": "2026-07-01",
            "currency": "EUR",
            "exchange_rate": "1.10",
            "lines": [
                {
                    "account_id": expense.id,
                    "description": "supplies",
                    "quantity": 1,
                    "rate": 100,
                }
            ],
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def test_cross_currency_bill_payment_rejected(client, eur_bill):
    resp = client.post(
        "/api/bill-payments",
        json={
            "vendor_id": eur_bill["vendor_id"],
            "date": "2026-07-10",
            "amount": 100,
            "currency": "USD",
            "allocations": [{"bill_id": eur_bill["id"], "amount": 100}],
        },
    )
    assert resp.status_code == 400
    assert "does not match" in resp.json()["detail"]


def _bill_payment_fx_lines(db_session):
    fx_acct = db_session.query(Account).filter(Account.account_number == "6999").first()
    txn = (
        db_session.query(Transaction)
        .filter(Transaction.source_type == "bill_payment")
        .order_by(Transaction.id.desc())
        .first()
    )
    lines = db_session.query(TransactionLine).filter_by(transaction_id=txn.id).all()
    assert sum(ln.debit or 0 for ln in lines) == sum(ln.credit or 0 for ln in lines)
    if fx_acct is None:
        return []  # FX account never created — no FX activity at all
    return [ln for ln in lines if ln.account_id == fx_acct.id]


def test_bill_payment_realizes_fx_gain(client, db_session, eur_bill):
    """Bill booked at 1.10 (A/P 110 home); settled at 1.05 (cash 105)
    → 5.00 gain (credit) — paying cheaper than booked."""
    resp = client.post(
        "/api/bill-payments",
        json={
            "vendor_id": eur_bill["vendor_id"],
            "date": "2026-07-10",
            "amount": 100,
            "currency": "EUR",
            "exchange_rate": "1.05",
            "allocations": [{"bill_id": eur_bill["id"], "amount": 100}],
        },
    )
    assert resp.status_code in (200, 201), resp.text
    fx_lines = _bill_payment_fx_lines(db_session)
    assert len(fx_lines) == 1
    assert fx_lines[0].credit == Decimal("5.00")

    bill = client.get(f"/api/bills/{eur_bill['id']}").json()
    assert bill["status"] == "paid"
    assert Decimal(str(bill["balance_due"])) == Decimal("0.00")


def test_bill_payment_realizes_fx_loss(client, db_session, eur_bill):
    resp = client.post(
        "/api/bill-payments",
        json={
            "vendor_id": eur_bill["vendor_id"],
            "date": "2026-07-10",
            "amount": 100,
            "currency": "EUR",
            "exchange_rate": "1.20",
            "allocations": [{"bill_id": eur_bill["id"], "amount": 100}],
        },
    )
    assert resp.status_code in (200, 201), resp.text
    fx_lines = _bill_payment_fx_lines(db_session)
    assert fx_lines[0].debit == Decimal("10.00")


def test_home_currency_bill_payment_has_no_fx_line(client, db_session, seed_accounts):
    from app.models.contacts import Vendor

    vendor = Vendor(name="Home AP Vendor", is_active=True)
    db_session.add(vendor)
    db_session.commit()
    expense = db_session.query(Account).first()
    bill = client.post(
        "/api/bills",
        json={
            "vendor_id": vendor.id,
            "bill_number": "USD-AP-1",
            "date": "2026-07-01",
            "lines": [
                {
                    "account_id": expense.id,
                    "description": "x",
                    "quantity": 1,
                    "rate": 50,
                }
            ],
        },
    ).json()
    resp = client.post(
        "/api/bill-payments",
        json={
            "vendor_id": vendor.id,
            "date": "2026-07-10",
            "amount": 50,
            "allocations": [{"bill_id": bill["id"], "amount": 50}],
        },
    )
    assert resp.status_code in (200, 201), resp.text
    assert _bill_payment_fx_lines(db_session) == []
