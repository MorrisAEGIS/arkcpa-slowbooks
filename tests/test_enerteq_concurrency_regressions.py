from datetime import date
from decimal import Decimal

from app.models.invoices import Invoice
from app.models.job_costing import JobCost
from app.models.transactions import Transaction
from app.models.settings import Settings
from app.services.payments.recorder import record_provider_payment
from app.services.job_costing import void_job_cost
from app.routes.invoices.lifecycle import write_off_invoice, WriteOffRequest
from tests import test_enerteq_time_lifecycle as time_lifecycle

time_ticket_fixture = time_lifecycle.ticket
post = time_lifecycle.post


def issued(client, seed_customer, when="2026-10-10"):
    response = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": when,
            "due_date": when,
            "lines": [{"description": "Synthetic", "quantity": 1, "rate": 100}],
        },
    )
    assert response.status_code == 201, response.text
    doc = response.json()
    assert client.post(f"/api/invoices/{doc['id']}/issue").status_code == 200
    return doc


def test_provider_refetches_before_recording_after_concurrent_void(
    client, seed_accounts, seed_customer, TestSession
):
    doc = issued(client, seed_customer)
    with TestSession() as stale:
        cached = stale.get(Invoice, doc["id"])
        assert cached.status.value == "sent"
        assert client.post(f"/api/invoices/{doc['id']}/void").status_code == 200
        result = record_provider_payment(
            stale,
            "stripe",
            "Stripe",
            doc["id"],
            "synthetic-raced-payment",
            Decimal("100"),
        )
        assert result == "invoice_already_settled", result


def test_job_cost_refetches_before_second_void(
    client, db_session, TestSession, request
):
    result = post(client, request.getfixturevalue("time_ticket_fixture"))
    with TestSession() as stale:
        cached = stale.get(JobCost, result["job_cost_id"])
        assert cached.status != "void"
        assert client.post(f"/api/job-costs/{cached.id}/void").status_code == 200
        try:
            void_job_cost(stale, cached)
            stale.commit()
        except ValueError:
            stale.rollback()
    db_session.expire_all()
    assert (
        db_session.query(Transaction)
        .filter_by(source_type="job_cost_void", source_id=result["job_cost_id"])
        .count()
        == 1
    )


def test_writeoff_refetches_before_acting_on_concurrently_voided_invoice(
    client, seed_accounts, seed_customer, TestSession
):
    from fastapi import HTTPException

    doc = issued(client, seed_customer)
    with TestSession() as stale:
        cached = stale.get(Invoice, doc["id"])
        assert cached.status.value == "sent"
        assert client.post(f"/api/invoices/{doc['id']}/void").status_code == 200
        try:
            write_off_invoice(
                doc["id"], WriteOffRequest(date=date(2026, 10, 10)), stale
            )
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            assert False, "Writeoff accepted a previously voided invoice"


def test_void_invoice_with_late_fee_leaves_no_orphan_receivable(
    client, db_session, seed_accounts, seed_customer
):
    doc = issued(client, seed_customer, "2026-01-01")
    for key, value in (
        ("late_fee_enabled", "true"),
        ("late_fee_rate", "10"),
        ("late_fee_grace_days", "0"),
    ):
        db_session.add(Settings(key=key, value=value))
    db_session.commit()
    response = client.post("/api/invoices/apply-late-fees")
    assert response.status_code == 200, response.text
    assert response.json()["applied"] == 1
    response = client.post(f"/api/invoices/{doc['id']}/void")
    db_session.expire_all()
    assert response.status_code == 200, response.text
    if response.status_code == 200:
        assert (
            seed_accounts["1100"].balance == 0
        ), f"Orphan receivable after void: {seed_accounts['1100'].balance}"


def test_multiple_allocations_same_invoice_preserve_or_reject_atomically(
    client, db_session, seed_accounts, seed_customer
):
    doc = issued(client, seed_customer)
    response = client.post(
        "/api/payments",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-10-10",
            "amount": "100",
            "allocations": [
                {"invoice_id": doc["id"], "amount": "40"},
                {"invoice_id": doc["id"], "amount": "60"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    db_session.expire_all()
    invoice = db_session.get(Invoice, doc["id"])
    assert invoice.amount_paid == 100 and invoice.balance_due == 0
    response = client.post(f"/api/payments/{response.json()['id']}/void")
    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert invoice.amount_paid == 0 and invoice.balance_due == 100
