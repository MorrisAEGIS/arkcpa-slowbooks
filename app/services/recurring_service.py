# ============================================================================
# Recurring Invoice Service — generates invoices from recurring templates
# Feature 2: Infrastructure C (background scheduler / cron)
# ============================================================================

from datetime import date, timedelta
from decimal import Decimal
from dateutil.relativedelta import relativedelta

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models.recurring import RecurringInvoice
from app.models.invoices import Invoice, InvoiceLine
from app.services.numbering import next_invoice_number
from app.services.accounting import (
    _q,
    compute_line_totals,
)


def _advance_next_due(current: date, frequency: str) -> date:
    if frequency == "weekly":
        return current + timedelta(weeks=1)
    elif frequency == "monthly":
        return current + relativedelta(months=1)
    elif frequency == "quarterly":
        return current + relativedelta(months=3)
    elif frequency == "yearly":
        return current + relativedelta(years=1)
    return current + relativedelta(months=1)


def generate_due_invoices(db: Session, as_of: date = None) -> list[int]:
    """Generate all invoices that are due on or before as_of date.
    Returns list of created invoice IDs."""
    from app.services.settings_service import is_nonprofit

    # A nonprofit's recurring invoices are pledges (printed as such)
    nonprofit = is_nonprofit(db)
    if as_of is None:
        as_of = date.today()

    recurrings = (
        db.query(RecurringInvoice)
        .filter(
            RecurringInvoice.is_active,
            RecurringInvoice.next_due <= as_of,
        )
        .with_for_update()
        .populate_existing()
        .all()
    )

    created_ids = []

    for rec in recurrings:
        # Check end date
        if rec.end_date and rec.next_due > rec.end_date:
            rec.is_active = False
            continue

        # Compute totals via the shared helper: each line is rounded to 2dp
        # before summing so the stored subtotal matches the sum of stored
        # line amounts, and journal credits land on the same cents as the
        # A/R debit once SQL rounds.
        tax_rate = rec.tax_rate or Decimal("0")
        subtotal, tax_amount, total = compute_line_totals(rec.lines, tax_rate)

        # Parse terms for due date
        due_date = rec.next_due + timedelta(days=30)
        if rec.terms:
            try:
                days = int(rec.terms.lower().replace("net ", ""))
                due_date = rec.next_due + timedelta(days=days)
            except ValueError:
                pass

        # MAX+1 numbering races against concurrent manual creates (same as
        # the invoices route). Retry under a SAVEPOINT so a collision rolls
        # back only this attempt — not the other invoices already generated
        # in this batch — and a template that can't get a number is simply
        # left for the next run instead of aborting the whole batch.
        invoice = None
        invoice_number = None
        for _ in range(10):
            invoice_number = next_invoice_number(db)
            candidate = Invoice(
                invoice_number=invoice_number,
                customer_id=rec.customer_id,
                date=rec.next_due,
                due_date=due_date,
                terms=rec.terms,
                subtotal=subtotal,
                tax_rate=tax_rate,
                tax_amount=tax_amount,
                total=total,
                balance_due=Decimal("0"),
                notes=rec.notes,
                class_id=rec.class_id,
                job_id=rec.job_id,
                recurring_invoice_id=rec.id,
                is_pledge=nonprofit,
            )
            nested = db.begin_nested()
            db.add(candidate)
            try:
                db.flush()
                nested.commit()
                invoice = candidate
                break
            except IntegrityError as e:
                nested.rollback()
                if "invoice_number" not in str(e.orig).lower():
                    raise
        if invoice is None:
            continue

        for rline in rec.lines:
            db.add(
                InvoiceLine(
                    invoice_id=invoice.id,
                    item_id=rline.item_id,
                    description=rline.description,
                    quantity=rline.quantity,
                    rate=rline.rate,
                    is_taxable=rline.is_taxable,
                    amount=_q(Decimal(str(rline.quantity)) * Decimal(str(rline.rate))),
                    line_order=rline.line_order,
                )
            )

        # Advance next due date
        rec.next_due = _advance_next_due(rec.next_due, rec.frequency)
        rec.invoices_created += 1
        if rec.end_date and rec.next_due > rec.end_date:
            rec.is_active = False

        created_ids.append(invoice.id)

    db.commit()
    return created_ids
