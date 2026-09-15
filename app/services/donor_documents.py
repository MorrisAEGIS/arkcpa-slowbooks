"""Donor documents — what a nonprofit hands the people who give.

The IRS language lives here once (Publication 1771): a written
acknowledgment states the amount, the date, and either that no goods or
services were provided in exchange, or a description and good-faith
estimate of what was (the gala dinner) so the deductible portion is
clear. For property the charity describes it and never values it. The
donation receipt, the acknowledgment letter and the year-end giving
statement all read from ``irs_statement``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.services.accounting import _q
from app.services.terminology import Terms, terms_for

NO_GOODS = "No goods or services were provided in exchange for this contribution."
RETAIN = "Please retain this acknowledgment for your tax records."


def _money(v) -> str:
    v = _q(Decimal(str(v or 0)))
    return f"${v:,.2f}"


def irs_statement(
    company: dict,
    amount,
    fair_value_amount=None,
    fair_value_description: Optional[str] = None,
    in_kind_descriptions: Optional[list[str]] = None,
) -> dict:
    """{deductible_amount, text} for one gift.

    amount               the contribution (None for property)
    fair_value_amount    value of goods/services the donor received back
    in_kind_descriptions the property given, when the gift is not cash
    """
    name = (company or {}).get("company_name") or "the organization"
    ein = (company or {}).get("company_tax_id") or ""
    tail = f" {name}" + (f" (EIN {ein})" if ein else "") + f". {RETAIN}"

    if in_kind_descriptions:
        what = "; ".join(d for d in in_kind_descriptions if d)
        text = (
            f"Thank you for your gift of {what}. {NO_GOODS} {name} has not "
            "assigned a value to this gift; the donor is responsible for "
            "determining its fair market value."
        ) + tail
        return {"deductible_amount": None, "text": text}

    amount = _q(Decimal(str(amount or 0)))
    fv = _q(Decimal(str(fair_value_amount or 0)))
    if fv > 0:
        desc = f" ({fair_value_description})" if fair_value_description else ""
        deductible = max(amount - fv, Decimal("0"))
        text = (
            f"In exchange for this contribution of {_money(amount)}, {name} "
            f"provided goods or services with an estimated fair market value "
            f"of {_money(fv)}{desc}. The portion of your contribution "
            f"deductible for federal income tax purposes is limited to "
            f"{_money(deductible)}."
        ) + tail
        return {"deductible_amount": float(deductible), "text": text}
    text = f"{NO_GOODS} Contribution of {_money(amount)} received by" + tail
    return {"deductible_amount": float(amount), "text": text}


def invoice_doc_kind(inv, t: Terms) -> str:
    """The printed document's name: SalesReceipt / Invoice for a business;
    DonationReceipt / Pledge / Invoice for a nonprofit (a nonprofit still
    invoices program fees and rentals, so only a flagged pledge prints as
    one)."""
    if not t.is_nonprofit:
        return "SalesReceipt" if inv.is_sales_receipt else "Invoice"
    if inv.is_sales_receipt:
        return "DonationReceipt"
    return "Pledge" if getattr(inv, "is_pledge", False) else "Invoice"


def invoice_pdf_context(inv, company: dict) -> dict:
    """Extra template context for invoice_pdf.html: the document kind and,
    on a nonprofit donation receipt, the IRS acknowledgment block."""
    t = terms_for(company)
    kind = invoice_doc_kind(inv, t)
    irs = None
    if kind == "DonationReceipt":
        irs = irs_statement(
            company,
            inv.total,
            getattr(inv, "fair_value_amount", None),
            getattr(inv, "fair_value_description", None),
        )
    return {"doc_kind": kind, "irs": irs}


# ── gifts: one shape for a receipt, a payment, or property ───────────────

ACK_TEMPLATE_NAME = "donation_acknowledgment"
ACK_SUBJECT = "Thank you for your gift to {{ company.company_name }}"
ACK_BODY = """<p>{{ donor.salutation or ('Dear ' ~ donor_name) }},</p>
<p>Thank you for your {% if gift.in_kind_lines %}generous gift{% else %}contribution of {{ gift.amount | currency }}{% endif %} on {{ gift.date | fdate }}{% if gift.number %} ({{ gift.number }}){% endif %}. Your support makes our work possible.</p>
<p>{{ irs.text }}</p>
<p>With gratitude,</p>
<p>{{ company.company_name }}</p>"""


class NotAGift(ValueError):
    """This document does not get an acknowledgment; `reason` says why, in
    the user's words (a fixed phrase, never exception text)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def gift_from_invoice(inv) -> dict:
    """A sales receipt is a gift; a pledge is not until it is paid."""
    if not inv.is_sales_receipt:
        raise NotAGift("Acknowledge the payment, not the pledge")
    return {
        "kind": "invoice",
        "id": inv.id,
        "number": inv.invoice_number,
        "date": inv.date,
        "amount": _q(Decimal(str(inv.total or 0))),
        "description": ", ".join(ln.description for ln in inv.lines if ln.description),
        "fair_value_amount": inv.fair_value_amount,
        "fair_value_description": inv.fair_value_description,
        "in_kind_lines": [],
        "customer_id": inv.customer_id,
    }


def payment_gift_amount(db, payment) -> Decimal:
    """The part of a payment that is a gift: everything except what it
    paid on a sales receipt (that receipt is acknowledged on its own)."""
    from app.models.invoices import Invoice
    from app.models.payments import PaymentAllocation

    on_receipts = (
        db.query(PaymentAllocation)
        .join(Invoice, Invoice.id == PaymentAllocation.invoice_id)
        .filter(
            PaymentAllocation.payment_id == payment.id,
            Invoice.is_sales_receipt.is_(True),
        )
        .all()
    )
    total = _q(Decimal(str(payment.amount or 0)))
    receipts = sum((Decimal(str(a.amount)) for a in on_receipts), Decimal("0"))
    return _q(max(total - receipts, Decimal("0")))


def gift_from_payment(db, payment) -> dict:
    if getattr(payment, "is_voided", False):
        raise NotAGift("This payment is void")
    amount = payment_gift_amount(db, payment)
    if amount <= 0:
        raise NotAGift(
            "This payment belongs to a donation receipt — acknowledge the receipt"
        )
    return {
        "kind": "payment",
        "id": payment.id,
        "number": payment.reference or payment.check_number or f"payment {payment.id}",
        "date": payment.date,
        "amount": amount,
        "description": payment.notes or "",
        "fair_value_amount": None,
        "fair_value_description": None,
        "in_kind_lines": [],
        "customer_id": payment.customer_id,
    }


def gift_from_in_kind(gift) -> dict:
    if gift.status == "void":
        raise NotAGift("This in-kind gift is void")
    return {
        "kind": "in-kind",
        "id": gift.id,
        "number": gift.number,
        "date": gift.date,
        "amount": None,
        "description": gift.memo or "",
        "fair_value_amount": None,
        "fair_value_description": None,
        "in_kind_lines": [
            {"description": ln.description, "quantity": ln.quantity}
            for ln in gift.lines
        ],
        "customer_id": gift.customer_id,
    }


def load_gift(db, kind: str, gift_id: int) -> dict:
    """Resolve a (kind, id) pair to the normalized gift dict."""
    if kind == "invoice":
        from app.models.invoices import Invoice

        inv = db.get(Invoice, gift_id)
        if inv is None:
            raise LookupError("Receipt not found")
        return gift_from_invoice(inv)
    if kind == "payment":
        from app.models.payments import Payment

        pmt = db.get(Payment, gift_id)
        if pmt is None:
            raise LookupError("Payment not found")
        return gift_from_payment(db, pmt)
    if kind == "in-kind":
        from app.models.in_kind import InKindGift

        ik = db.get(InKindGift, gift_id)
        if ik is None:
            raise LookupError("In-kind gift not found")
        return gift_from_in_kind(ik)
    raise LookupError("Unknown gift kind")


def gift_irs(company: dict, gift: dict) -> dict:
    if gift["in_kind_lines"]:
        descs = [
            (f"{ln['quantity']:g} × " if ln.get("quantity") not in (None, 1) else "")
            + ln["description"]
            for ln in gift["in_kind_lines"]
        ]
        return irs_statement(company, None, in_kind_descriptions=descs)
    return irs_statement(
        company,
        gift["amount"],
        gift["fair_value_amount"],
        gift["fair_value_description"],
    )


def render_acknowledgment(db, company: dict, customer, gift: dict) -> tuple[str, str]:
    """(subject, body_html) from the editable email template
    'donation_acknowledgment' (Settings -> Email Templates), falling back
    to the built-in text when the row has not been seeded. Rendered in the
    sandboxed environment, autoescaped."""
    from app.services.settings_service import redact_secrets

    from jinja2.sandbox import SandboxedEnvironment

    from app.services.email_service import render_template_from_db
    from app.services.pdf_service import _format_currency, _format_date

    context = {
        "donor": customer,
        "donor_name": customer.name,
        "customer_name": customer.name,
        # Editable templates must never receive decrypted service credentials.
        "company": redact_secrets(company),
        "gift": gift,
        "irs": gift_irs(company, gift),
    }
    subject, body = render_template_from_db(db, ACK_TEMPLATE_NAME, context)
    if subject is None:
        env = SandboxedEnvironment(autoescape=True)
        env.filters["currency"] = _format_currency
        env.filters["fdate"] = _format_date
        subject = env.from_string(ACK_SUBJECT).render(**context)
        body = env.from_string(ACK_BODY).render(**context)
    return subject, body


# ── year-end giving statement ────────────────────────────────────────────


def collect_gifts(db, customer_id: int, year: int) -> dict:
    """Everything a donor gave in a calendar year: cash (donation receipts
    plus payments that are gifts — a receipt's own payment is never
    counted twice) with the deductible portion per gift, and non-cash
    gifts listed without a value."""
    from datetime import date as _date

    from app.models.in_kind import InKindGift
    from app.models.invoices import Invoice, InvoiceStatus
    from app.models.payments import Payment

    start, end = _date(year, 1, 1), _date(year, 12, 31)
    cash: list[dict] = []
    receipts = (
        db.query(Invoice)
        .filter(
            Invoice.customer_id == customer_id,
            Invoice.is_sales_receipt.is_(True),
            Invoice.status != InvoiceStatus.VOID,
            Invoice.date >= start,
            Invoice.date <= end,
        )
        .order_by(Invoice.date, Invoice.id)
        .all()
    )
    for inv in receipts:
        cash.append(gift_from_invoice(inv))
    payments = (
        db.query(Payment)
        .filter(
            Payment.customer_id == customer_id,
            Payment.is_voided.is_(False),
            Payment.date >= start,
            Payment.date <= end,
        )
        .order_by(Payment.date, Payment.id)
        .all()
    )
    for pmt in payments:
        amount = payment_gift_amount(db, pmt)
        if amount > 0:
            g = gift_from_payment(db, pmt)
            cash.append(g)
    cash.sort(key=lambda g: (g["date"], g["kind"], g["id"]))
    for g in cash:
        fv = _q(Decimal(str(g["fair_value_amount"] or 0)))
        g["deductible"] = _q(max(g["amount"] - fv, Decimal("0")))
    in_kind = [
        gift_from_in_kind(ik)
        for ik in db.query(InKindGift)
        .filter(
            InKindGift.customer_id == customer_id,
            InKindGift.status == "posted",
            InKindGift.date >= start,
            InKindGift.date <= end,
        )
        .order_by(InKindGift.date, InKindGift.id)
        .all()
    ]
    total = sum((g["amount"] for g in cash), Decimal("0"))
    fair_value = sum(
        (_q(Decimal(str(g["fair_value_amount"] or 0))) for g in cash), Decimal("0")
    )
    return {
        "cash": cash,
        "in_kind": in_kind,
        "totals": {
            "amount": _q(total),
            "fair_value": _q(fair_value),
            "deductible": _q(total - fair_value),
        },
    }


def giving_statement_irs_text(company: dict, totals: dict) -> str:
    name = (company or {}).get("company_name") or "the organization"
    ein = (company or {}).get("company_tax_id") or ""
    base = (
        f"This statement summarizes contributions received by {name}"
        + (f" (EIN {ein})" if ein else "")
        + " during the year. "
    )
    if totals["fair_value"] > 0:
        return base + (
            "Except where a value of goods or services is shown above, no goods "
            "or services were provided in exchange for these contributions; where "
            "one is shown, the deductible portion is limited to the amount in the "
            "Deductible column. " + RETAIN
        )
    return base + NO_GOODS + " " + RETAIN
