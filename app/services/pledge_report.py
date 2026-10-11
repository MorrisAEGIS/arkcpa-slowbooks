"""Pledge report — promised, invoiced, received, written off, outstanding.

A recurring invoice is the pledge (monthly $100 from January); the
invoices it generated are the installments; payments on them are what
came in; a write-off credit memo is what was forgiven. A one-off pledge
is an invoice flagged is_pledge with no template. Rows are grouped by
donor and by campaign (class) and every column reconciles: pledged =
invoiced + not yet invoiced; invoiced = received + written off +
outstanding.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from app.models.classes import TxnClass
from app.models.contacts import Customer
from app.models.credit_memos import CreditApplication, CreditMemo, CreditMemoStatus
from app.models.invoices import Invoice
from app.services.invoice_lifecycle import issued_invoice_filter
from app.models.payments import Payment, PaymentAllocation
from app.models.recurring import RecurringInvoice
from app.services.accounting import _q, compute_line_totals
from app.services.recurring_service import _advance_next_due

ZERO = Decimal("0")


def scheduled_installments(rec: RecurringInvoice, start: date, end: date) -> int:
    """How many installments the template promises inside [start, end]."""
    n = 0
    d = rec.start_date
    stop = min(end, rec.end_date) if rec.end_date else end
    guard = 0
    while d <= stop and guard < 5000:
        if d >= start:
            n += 1
        nxt = _advance_next_due(d, rec.frequency)
        if nxt <= d:
            break
        d = nxt
        guard += 1
    return n


def installment_amount(rec: RecurringInvoice) -> Decimal:
    _sub, _tax, total = compute_line_totals(rec.lines, rec.tax_rate or 0)
    return _q(Decimal(str(total)))


def _sums_for_invoices(db: Session, invoice_ids: list[int]) -> dict:
    if not invoice_ids:
        return {"received": ZERO, "written_off": ZERO}
    received = (
        db.query(sqlfunc.coalesce(sqlfunc.sum(PaymentAllocation.amount), 0))
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .filter(
            PaymentAllocation.invoice_id.in_(invoice_ids),
            Payment.is_voided.is_(False),
        )
        .scalar()
    )
    written_off = (
        db.query(sqlfunc.coalesce(sqlfunc.sum(CreditApplication.amount), 0))
        .join(CreditMemo, CreditMemo.id == CreditApplication.credit_memo_id)
        .filter(
            CreditApplication.invoice_id.in_(invoice_ids),
            CreditMemo.is_write_off.is_(True),
            CreditMemo.status != CreditMemoStatus.VOID,
        )
        .scalar()
    )
    return {
        "received": _q(Decimal(str(received or 0))),
        "written_off": _q(Decimal(str(written_off or 0))),
    }


def _row(
    *,
    key: str,
    donor: Customer,
    class_id: Optional[int],
    class_name: str,
    label: str,
    pledged: Decimal,
    invoices: list[Invoice],
    sums: dict,
) -> dict:
    invoiced = sum((Decimal(str(i.total or 0)) for i in invoices), ZERO)
    outstanding = sum((Decimal(str(i.balance_due or 0)) for i in invoices), ZERO)
    return {
        "key": key,
        "customer_id": donor.id,
        "customer_name": donor.name,
        "class_id": class_id,
        "class_name": class_name,
        "label": label,
        "pledged": float(_q(pledged)),
        "invoiced": float(_q(invoiced)),
        "not_yet_invoiced": float(_q(max(pledged - invoiced, ZERO))),
        "received": float(sums["received"]),
        "written_off": float(sums["written_off"]),
        "outstanding": float(_q(outstanding)),
        "installments": len(invoices),
    }


def pledge_report(
    db: Session,
    start: date,
    end: date,
    class_id: Optional[int] = None,
    customer_id: Optional[int] = None,
) -> dict:
    names = {c.id: c.name for c in db.query(TxnClass.id, TxnClass.name).all()}
    rows: list[dict] = []

    q = db.query(RecurringInvoice).options(
        joinedload(RecurringInvoice.customer), joinedload(RecurringInvoice.lines)
    )
    if class_id is not None:
        q = q.filter(RecurringInvoice.class_id == class_id)
    if customer_id is not None:
        q = q.filter(RecurringInvoice.customer_id == customer_id)
    templates = q.all()
    for rec in templates:
        installments = scheduled_installments(rec, start, end)
        invoices = (
            db.query(Invoice)
            .filter(
                Invoice.recurring_invoice_id == rec.id,
                issued_invoice_filter(),
                Invoice.date >= start,
                Invoice.date <= end,
            )
            .all()
        )
        if installments == 0 and not invoices:
            continue
        pledged = installment_amount(rec) * installments
        rows.append(
            _row(
                key=f"recurring:{rec.id}",
                donor=rec.customer,
                class_id=rec.class_id,
                class_name=names.get(rec.class_id, "Uncategorized"),
                label=f"{rec.frequency} pledge from {rec.start_date}",
                pledged=pledged,
                invoices=invoices,
                sums=_sums_for_invoices(db, [i.id for i in invoices]),
            )
        )

    # one-off pledges: flagged invoices with no template
    q = (
        db.query(Invoice)
        .options(joinedload(Invoice.customer))
        .filter(
            Invoice.is_pledge.is_(True),
            Invoice.recurring_invoice_id.is_(None),
            issued_invoice_filter(),
            Invoice.date >= start,
            Invoice.date <= end,
        )
    )
    if class_id is not None:
        q = q.filter(Invoice.class_id == class_id)
    if customer_id is not None:
        q = q.filter(Invoice.customer_id == customer_id)
    for inv in q.all():
        rows.append(
            _row(
                key=f"invoice:{inv.id}",
                donor=inv.customer,
                class_id=inv.class_id,
                class_name=names.get(inv.class_id, "Uncategorized"),
                label=f"Pledge {inv.invoice_number}",
                pledged=Decimal(str(inv.total or 0)),
                invoices=[inv],
                sums=_sums_for_invoices(db, [inv.id]),
            )
        )

    cols = (
        "pledged",
        "invoiced",
        "not_yet_invoiced",
        "received",
        "written_off",
        "outstanding",
    )

    def _group(key_fn, name_fn):
        groups: dict = {}
        for r in rows:
            g = groups.setdefault(
                key_fn(r),
                {"name": name_fn(r), "pledges": [], **{c: ZERO for c in cols}},
            )
            g["pledges"].append(r)
            for c in cols:
                g[c] += Decimal(str(r[c]))
        out = []
        for k, g in sorted(
            groups.items(), key=lambda kv: (kv[1]["name"] or "").lower()
        ):
            out.append(
                {
                    "id": k,
                    **{c: float(_q(g[c])) for c in cols},
                    "name": g["name"],
                    "pledges": g["pledges"],
                }
            )
        return out

    by_donor = [
        {**g, "customer_id": g.pop("id"), "customer_name": g.pop("name")}
        for g in _group(lambda r: r["customer_id"], lambda r: r["customer_name"])
    ]
    by_class = [
        {**g, "class_id": g.pop("id"), "class_name": g.pop("name")}
        for g in _group(lambda r: r["class_id"], lambda r: r["class_name"])
    ]
    for g in by_class:
        g["pledges"] = [p["key"] for p in g["pledges"]]
    totals = {c: float(_q(sum((Decimal(str(r[c])) for r in rows), ZERO))) for c in cols}
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "by_donor": by_donor,
        "by_class": by_class,
        "totals": totals,
    }
