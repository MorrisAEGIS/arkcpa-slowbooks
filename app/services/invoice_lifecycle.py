"""Explicit invoice issue boundary. Callers commit once after all posting succeeds.

No data migration: historical drafts with a journal are already posted and must
never be posted again. New drafts have zero receivable balance and no movements.
"""

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.invoices import Invoice, InvoiceStatus
from app.models.items import InventoryMovement, Item
from app.services.accounting import (
    _q,
    create_journal_entry,
    get_ar_account_id,
    get_default_income_account_id,
    get_sales_tax_account_id,
)
from app.services.closing_date import check_closing_date
from app.services.currency import convert_lines


def stored_invoice_totals(invoice: Invoice):
    """Book the reviewed line amounts; legacy rate columns store only 2dp.

    Recomputing quantity * persisted rate would change sub-cent-priced drafts
    after save. The rounded line amount is the existing accounting source.
    """
    subtotal = _q(sum((line.amount for line in invoice.lines), Decimal("0")))
    taxable = sum(
        (line.amount for line in invoice.lines if line.is_taxable is not False),
        Decimal("0"),
    )
    tax = _q(taxable * (invoice.tax_rate or Decimal("0")))
    return subtotal, tax, subtotal + tax


def issued_invoice_filter():
    """Include legacy posted drafts while excluding new unissued drafts."""
    return or_(
        Invoice.status.in_(
            [InvoiceStatus.SENT, InvoiceStatus.PARTIAL, InvoiceStatus.PAID]
        ),
        (Invoice.status == InvoiceStatus.DRAFT) & Invoice.transaction_id.is_not(None),
    )


def require_issued(invoice: Invoice) -> None:
    if invoice.status == InvoiceStatus.VOID or (
        invoice.status == InvoiceStatus.DRAFT and invoice.transaction_id is None
    ):
        raise HTTPException(
            status_code=409, detail="Issue the invoice before using this action"
        )


def has_inventory_posting(db: Session, invoice: Invoice) -> bool:
    return (
        db.query(InventoryMovement.id)
        .filter(
            InventoryMovement.source_type.in_(
                ["invoice", "invoice_edit", "invoice_void"]
            ),
            InventoryMovement.source_id == invoice.id,
        )
        .first()
        is not None
    )


def require_editable_draft(db: Session, invoice: Invoice) -> None:
    if (
        invoice.status != InvoiceStatus.DRAFT
        or invoice.transaction_id is not None
        or (invoice.amount_paid or Decimal("0")) != 0
        or has_inventory_posting(db, invoice)
    ):
        raise HTTPException(
            status_code=409,
            detail="Issued or previously posted invoices are locked. Reverse payments "
            "and void the invoice, then duplicate it into a corrected draft.",
        )


def issue_invoice(db: Session, invoice_id: int) -> Invoice:
    """Issue once under a row lock; retries return the existing issued invoice.

    The status, receivable and stock effects commit together. The caller must not
    commit a partially failed call. PostgreSQL serializes concurrent callers here;
    SQLite tests demonstrate sequential retry behavior, not database row locking.
    """
    invoice = (
        db.query(Invoice)
        .filter(Invoice.id == invoice_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == InvoiceStatus.VOID:
        raise HTTPException(status_code=409, detail="Cannot issue a void invoice")
    if invoice.status != InvoiceStatus.DRAFT:
        return invoice
    if invoice.transaction_id is not None:
        # Old releases posted on create while still labeling the document draft.
        # Keep the booked amount, rate and stock exactly as they are.
        invoice.status = InvoiceStatus.SENT
        return invoice
    if has_inventory_posting(db, invoice) or (invoice.amount_paid or 0) != 0:
        raise HTTPException(
            status_code=409,
            detail="Invoice has prior financial activity without a linked journal; "
            "reconcile it before issue. No additional posting was made.",
        )
    check_closing_date(db, invoice.date)
    if not invoice.lines:
        raise HTTPException(status_code=409, detail="Invoice needs at least one line")
    totals = stored_invoice_totals(invoice)
    if totals != (invoice.subtotal, invoice.tax_amount, invoice.total):
        raise HTTPException(
            status_code=409, detail="Invoice totals require review before issue"
        )

    ar_id = get_ar_account_id(db)
    income_id = get_default_income_account_id(db)
    tax_id = get_sales_tax_account_id(db) if invoice.tax_amount > 0 else None
    # Lock stock in a deterministic order before reading quantities/costs.
    item_ids = sorted({line.item_id for line in invoice.lines if line.item_id})
    items = {
        item.id: item
        for item in db.query(Item)
        .filter(Item.id.in_(item_ids))
        .order_by(Item.id)
        .with_for_update()
        .populate_existing()
        .all()
    }
    if len(items) != len(item_ids):
        raise HTTPException(status_code=409, detail="An invoice item no longer exists")
    journal = [{"account_id": ar_id, "debit": invoice.total, "credit": Decimal("0")}]
    for line in invoice.lines:
        item = items.get(line.item_id)
        journal.append(
            {
                "account_id": (item.income_account_id if item else None) or income_id,
                "debit": Decimal("0"),
                "credit": line.amount,
                "description": line.description or "",
                "job_id": line.job_id,
                "class_id": line.class_id,
                "cost_code_id": line.cost_code_id,
            }
        )
    if tax_id is not None:
        journal.append(
            {
                "account_id": tax_id,
                "debit": Decimal("0"),
                "credit": invoice.tax_amount,
                "description": "Sales tax",
            }
        )
    txn = create_journal_entry(
        db,
        invoice.date,
        f"Invoice #{invoice.invoice_number}",
        convert_lines(journal, invoice.exchange_rate or Decimal("1")),
        source_type="invoice",
        source_id=invoice.id,
        class_id=invoice.class_id,
        job_id=invoice.job_id,
        reference=invoice.invoice_number,
    )
    invoice.transaction_id = txn.id
    invoice.status = InvoiceStatus.SENT
    invoice.balance_due = invoice.total
    from app.services.inventory_hooks import post_sale_for_invoice

    post_sale_for_invoice(db, invoice, txn_date=invoice.date)
    return invoice
