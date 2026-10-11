"""Synthetic accounting acceptance: draft -> issue -> retry -> correction.

These tests use disposable SQLite, with no external provider calls or real books.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.models.invoices import Invoice, InvoiceStatus
from app.models.items import Item, ItemType, InventoryMovement
from app.models.settings import Settings
from app.models.transactions import Transaction
from app.services.invoice_lifecycle import issue_invoice


@pytest.fixture
def material(db_session, seed_accounts):
    item = Item(
        name="Synthetic conductor",
        item_type=ItemType.MATERIAL,
        rate=Decimal("10"),
        track_inventory=True,
        quantity_on_hand=Decimal("20"),
        avg_cost=Decimal("5"),
        income_account_id=seed_accounts["4000"].id,
        asset_account_id=seed_accounts["1300"].id,
    )
    db_session.add(item)
    db_session.commit()
    return item


def draft(client, customer_id, item_id=None, **extra):
    response = client.post(
        "/api/invoices",
        json={
            "customer_id": customer_id,
            "date": "2026-10-10",
            "tax_rate": "0.1",
            "lines": [{"item_id": item_id, "quantity": "2", "rate": "10"}],
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def counts(db):
    db.expire_all()
    return db.query(Transaction).count(), db.query(InventoryMovement).count()


def test_draft_edit_issue_retry_and_void_are_balanced(
    client, db_session, seed_customer, material, seed_accounts
):
    inv = draft(client, seed_customer.id, material.id)
    assert inv["status"] == "draft" and inv["transaction_id"] is None
    assert Decimal(inv["total"]) == 22 and Decimal(inv["balance_due"]) == 0
    assert counts(db_session) == (0, 0)
    assert material.quantity_on_hand == 20
    edit = client.put(
        f"/api/invoices/{inv['id']}",
        json={"lines": [{"item_id": material.id, "quantity": "3", "rate": "10"}]},
    )
    assert edit.status_code == 200, edit.text
    assert Decimal(edit.json()["total"]) == 33
    assert counts(db_session) == (0, 0)
    issued = client.post(f"/api/invoices/{inv['id']}/issue")
    assert issued.status_code == 200, issued.text
    assert Decimal(issued.json()["balance_due"]) == 33
    assert issued.json()["transaction_id"] is not None
    first = counts(db_session)
    assert first == (2, 1)
    assert material.quantity_on_hand == 17
    for action in ("issue", "send", "issue"):
        repeat = client.post(f"/api/invoices/{inv['id']}/{action}")
        assert repeat.status_code == 200, repeat.text
        assert repeat.json()["transaction_id"] == issued.json()["transaction_id"]
        assert counts(db_session) == first
    assert seed_accounts["1100"].balance == 33
    assert seed_accounts["5000"].balance == 15
    blocked = client.put(f"/api/invoices/{inv['id']}", json={"lines": [{"rate": "99"}]})
    assert blocked.status_code == 409
    assert counts(db_session) == first
    voided = client.post(f"/api/invoices/{inv['id']}/void")
    assert voided.status_code == 200, voided.text
    assert counts(db_session) == (4, 2)
    assert material.quantity_on_hand == 20
    assert seed_accounts["1100"].balance == 0
    assert seed_accounts["5000"].balance == 0
    assert client.post(f"/api/invoices/{inv['id']}/void").status_code == 200
    assert counts(db_session) == (4, 2)
    assert client.post(f"/api/invoices/{inv['id']}/issue").status_code == 409


def test_void_unissued_draft_never_adds_stock(
    client, db_session, seed_customer, material
):
    inv = draft(client, seed_customer.id, material.id)
    assert client.post(f"/api/invoices/{inv['id']}/void").status_code == 200
    assert counts(db_session) == (0, 0)
    assert material.quantity_on_hand == 20


def test_legacy_mixed_cost_stock_void_reverses_actual_history(
    client, db_session, seed_customer, material, seed_accounts
):
    from app.services.inventory_service import record_sale

    inv = draft(client, seed_customer.id, material.id)
    assert client.post(f"/api/invoices/{inv['id']}/issue").status_code == 200
    db_session.refresh(material)
    material.avg_cost = Decimal("7")
    # Model an older incremental edit, whose sale has a different unit cost.
    record_sale(
        db_session,
        material,
        Decimal("1"),
        "invoice",
        inv["id"],
        txn_date=date(2026, 10, 10),
    )
    material.track_inventory = False  # Historical custody must still be restored.
    db_session.commit()
    assert client.post(f"/api/invoices/{inv['id']}/void").status_code == 200
    db_session.expire_all()
    assert material.quantity_on_hand == 20
    assert seed_accounts["5000"].balance == 0
    reversals = (
        db_session.query(InventoryMovement)
        .filter_by(source_type="invoice_void", source_id=inv["id"])
        .all()
    )
    assert len(reversals) == 1 and reversals[0].quantity == 3
    assert reversals[0].transaction_id is not None


def test_new_draft_cannot_be_paid_emailed_or_status_promoted(
    client, db_session, seed_customer, material
):
    inv = draft(client, seed_customer.id, material.id)
    assert (
        client.put(f"/api/invoices/{inv['id']}", json={"status": "sent"}).status_code
        == 409
    )
    response = client.post(
        "/api/payments",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-10-10",
            "amount": "22",
            "allocations": [{"invoice_id": inv["id"], "amount": "22"}],
        },
    )
    assert response.status_code == 409, response.text
    assert (
        client.post(
            f"/api/invoices/{inv['id']}/email",
            json={"recipient": "synthetic@example.invalid"},
        ).status_code
        == 409
    )
    assert client.get(f"/api/payments/payment-link/{inv['id']}").status_code == 409
    assert counts(db_session) == (0, 0)


def test_legacy_posted_draft_is_not_reposted_or_edited(
    client, db_session, seed_customer, material
):
    inv = draft(client, seed_customer.id, material.id)
    assert client.post(f"/api/invoices/{inv['id']}/issue").status_code == 200
    row = db_session.get(Invoice, inv["id"])
    row.status = InvoiceStatus.DRAFT  # Fixture models pre-repair create behavior.
    db_session.commit()
    before = counts(db_session)
    assert (
        client.put(f"/api/invoices/{inv['id']}", json={"notes": "change"}).status_code
        == 409
    )
    assert client.post(f"/api/invoices/{inv['id']}/issue").status_code == 200
    assert counts(db_session) == before
    assert material.quantity_on_hand == 18


@pytest.mark.parametrize("amount,expected", [("12", 400), ("10", 201)])
def test_repeated_allocations_refresh_without_losing_pending_balance(
    client, db_session, seed_customer, material, amount, expected
):
    inv = draft(client, seed_customer.id, material.id)
    assert client.post(f"/api/invoices/{inv['id']}/issue").status_code == 200
    response = client.post(
        "/api/payments",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-10-10",
            "amount": str(Decimal(amount) * 2),
            "allocations": [{"invoice_id": inv["id"], "amount": amount}] * 2,
        },
    )
    assert response.status_code == expected, response.text
    db_session.expire_all()
    row = db_session.get(Invoice, inv["id"])
    if expected == 201:
        assert row.amount_paid == 20 and row.balance_due == 2
        voided = client.post(f"/api/payments/{response.json()['id']}/void")
        assert voided.status_code == 200, voided.text
        db_session.expire_all()
        assert row.amount_paid == 0 and row.balance_due == 22
    else:
        assert row.amount_paid == 0 and row.balance_due == 22


def test_duplicate_is_a_nonposting_draft_with_stored_currency(
    client, db_session, seed_customer, material
):
    inv = draft(
        client, seed_customer.id, material.id, currency="CAD", exchange_rate="0.75"
    )
    assert client.post(f"/api/invoices/{inv['id']}/issue").status_code == 200
    before = counts(db_session)
    duplicate = client.post(f"/api/invoices/{inv['id']}/duplicate")
    assert duplicate.status_code == 201, duplicate.text
    doc = duplicate.json()
    assert doc["status"] == "draft" and doc["transaction_id"] is None
    assert doc["currency"] == "CAD" and Decimal(doc["exchange_rate"]) == Decimal("0.75")
    assert Decimal(doc["balance_due"]) == 0
    assert counts(db_session) == before


def test_issue_failure_rolls_back_status_journal_and_stock(
    client, db_session, seed_customer, material, monkeypatch
):
    inv = draft(client, seed_customer.id, material.id)

    def fail_after_journal(*args, **kwargs):
        raise ValueError("synthetic stock failure")

    monkeypatch.setattr(
        "app.services.inventory_hooks.post_sale_for_invoice", fail_after_journal
    )
    with pytest.raises(ValueError, match="synthetic stock failure"):
        issue_invoice(db_session, inv["id"])
    db_session.rollback()
    assert counts(db_session) == (0, 0)
    row = db_session.get(Invoice, inv["id"])
    assert row.status == InvoiceStatus.DRAFT and row.transaction_id is None
    assert row.balance_due == 0 and material.quantity_on_hand == 20


def test_closing_period_after_draft_blocks_issue(
    client, db_session, seed_customer, material
):
    inv = draft(client, seed_customer.id, material.id)
    db_session.add(Settings(key="closing_date", value="2026-10-10"))
    db_session.commit()
    response = client.post(f"/api/invoices/{inv['id']}/issue")
    assert response.status_code == 403, response.text
    assert counts(db_session) == (0, 0)


def test_draft_excluded_from_income_statement_and_iif_export(
    client, db_session, seed_customer, material
):
    from app.services.iif_export import export_invoices

    inv = draft(client, seed_customer.id, material.id)
    report = client.get(
        "/api/reports/income-by-customer",
        params={"start_date": "2026-10-01", "end_date": "2026-10-31"},
    )
    assert report.status_code == 200, report.text
    assert report.json()["total_sales"] == 0
    exported = export_invoices(db_session, date(2026, 10, 1), date(2026, 10, 31))
    assert inv["invoice_number"] not in exported
    tax = client.get(
        "/api/reports/sales-tax",
        params={"start_date": "2026-10-01", "end_date": "2026-10-31"},
    )
    assert tax.status_code == 200 and tax.json()["total_tax"] == 0
    assert client.get(f"/pay/{inv['payment_token']}").status_code == 409


def test_issue_preserves_job_and_cost_code_on_reversal(
    client, db_session, seed_customer, material
):
    job = client.post(
        "/api/jobs",
        json={"customer_id": seed_customer.id, "name": "Synthetic line job"},
    ).json()
    code = client.post(
        "/api/cost-codes",
        json={"code": "MAT", "name": "Materials", "cost_type": "material"},
    ).json()
    inv = draft(
        client,
        seed_customer.id,
        material.id,
        job_id=job["id"],
        lines=[
            {
                "item_id": material.id,
                "quantity": "2",
                "rate": "10",
                "job_id": job["id"],
                "cost_code_id": code["id"],
            }
        ],
    )
    assert client.post(f"/api/invoices/{inv['id']}/issue").status_code == 200
    assert client.post(f"/api/invoices/{inv['id']}/void").status_code == 200
    from app.models.transactions import TransactionLine

    tagged = (
        db_session.query(TransactionLine)
        .filter(TransactionLine.cost_code_id == code["id"])
        .all()
    )
    assert len(tagged) == 2
    assert sum(line.debit - line.credit for line in tagged) == 0
    assert all(line.job_id == job["id"] for line in tagged)
