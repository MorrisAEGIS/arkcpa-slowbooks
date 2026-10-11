from decimal import Decimal

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.exc import IntegrityError

from app.database import get_db
from app.routes._helpers import clamp_pagination
from app.models.invoices import Invoice, InvoiceLine, InvoiceStatus
from app.models.contacts import Customer
from app.schemas.invoices import InvoiceCreate, InvoiceUpdate, InvoiceResponse
from app.services.accounting import (
    _q,
)
from app.services.numbering import next_invoice_number
from app.services.closing_date import check_closing_date

from app.routes.invoices._router import router
from app.routes.invoices.helpers import (
    resolve_line_taxable,
    _due_date_from_terms,
    _compute_totals,
)


@router.get("", response_model=list[InvoiceResponse])
def list_invoices(
    status: str = None,
    customer_id: int = None,
    is_sales_receipt: bool = None,
    skip: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db),
):
    skip, limit = clamp_pagination(skip, limit)
    # Eager-load customer (used for customer_name) and lines (in the
    # response model). Without these, returning 500 invoices triggered
    # 1001 extra queries — one per .customer access and one per .lines
    # access during model_validate.
    q = db.query(Invoice).options(
        joinedload(Invoice.customer),
        selectinload(Invoice.lines),
    )
    if status:
        q = q.filter(Invoice.status == status)
    if customer_id:
        q = q.filter(Invoice.customer_id == customer_id)
    if is_sales_receipt is not None:
        q = q.filter(Invoice.is_sales_receipt == is_sales_receipt)
    invoices = q.order_by(Invoice.date.desc()).offset(skip).limit(limit).all()
    results = []
    for inv in invoices:
        resp = InvoiceResponse.model_validate(inv)
        if inv.customer:
            resp.customer_name = inv.customer.name
        results.append(resp)
    return results


@router.get("/{invoice_id}", response_model=InvoiceResponse)
def get_invoice(invoice_id: int, db: Session = Depends(get_db)):
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    resp = InvoiceResponse.model_validate(inv)
    if inv.customer:
        resp.customer_name = inv.customer.name
    return resp


def _check_fair_value(fair_value_amount, total) -> None:
    """A donation receipt's fair-value-of-goods can't exceed the gift."""
    if fair_value_amount is None:
        return
    fv = Decimal(str(fair_value_amount))
    if fv < 0 or fv > Decimal(str(total)):
        raise HTTPException(
            status_code=400,
            detail="Fair value of goods or services must be between 0 and the total",
        )


@router.post("", response_model=InvoiceResponse, status_code=201)
def create_invoice(data: InvoiceCreate, db: Session = Depends(get_db)):
    check_closing_date(db, data.date)
    customer = db.query(Customer).filter(Customer.id == data.customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    # Parse terms for due date (explicit due_date wins; else derive from terms)
    due_date = data.due_date or _due_date_from_terms(data.date, data.terms)
    resolve_line_taxable(db, data.lines, customer)
    subtotal, tax_amount, total = _compute_totals(data.lines, data.tax_rate)
    _check_fair_value(data.fair_value_amount, total)

    # Capture every customer field we need post-flush, because we may have to
    # rollback the session (which expires `customer`) when two concurrent
    # requests both compute MAX+1 and race for the same invoice number.
    cust_id = customer.id
    cust_name = customer.name
    cust_fields = {
        "bill_address1": data.bill_address1 or customer.bill_address1,
        "bill_address2": data.bill_address2 or customer.bill_address2,
        "bill_city": data.bill_city or customer.bill_city,
        "bill_state": data.bill_state or customer.bill_state,
        "bill_zip": data.bill_zip or customer.bill_zip,
        "ship_address1": data.ship_address1 or customer.ship_address1,
        "ship_address2": data.ship_address2 or customer.ship_address2,
        "ship_city": data.ship_city or customer.ship_city,
        "ship_state": data.ship_state or customer.ship_state,
        "ship_zip": data.ship_zip or customer.ship_zip,
    }

    invoice = None
    invoice_number = None
    last_err = None
    # Retry the number assignment a few times — next_invoice_number is just
    # MAX+1 (no row-level lock), so two concurrent creates can both compute
    # the same number and one will hit the invoices.invoice_number UNIQUE
    # constraint at flush. The constraint is the safety net; this is the UX.
    from app.services.currency import resolve_rate

    doc_currency, doc_rate = resolve_rate(db, data.currency, data.exchange_rate)

    for _ in range(10):
        invoice_number = next_invoice_number(db)
        invoice = Invoice(
            invoice_number=invoice_number,
            currency=doc_currency,
            exchange_rate=doc_rate,
            customer_id=cust_id,
            date=data.date,
            due_date=due_date,
            terms=data.terms,
            po_number=data.po_number,
            subtotal=subtotal,
            tax_rate=data.tax_rate,
            tax_amount=tax_amount,
            total=total,
            balance_due=Decimal("0"),
            notes=data.notes,
            class_id=data.class_id,
            job_id=data.job_id,
            is_pledge=data.is_pledge,
            fair_value_amount=data.fair_value_amount,
            fair_value_description=data.fair_value_description,
            **cust_fields,
        )
        db.add(invoice)
        try:
            db.flush()
            break
        except IntegrityError as e:
            if "invoice_number" not in str(e.orig).lower():
                raise
            last_err = e
            db.rollback()
            invoice = None
    if invoice is None:
        raise HTTPException(
            status_code=503,
            detail="Could not assign a unique invoice number after several "
            "retries; please retry the request.",
        ) from last_err

    for i, line_data in enumerate(data.lines):
        line = InvoiceLine(
            invoice_id=invoice.id,
            item_id=line_data.item_id,
            description=line_data.description,
            quantity=line_data.quantity,
            rate=line_data.rate,
            amount=_q(Decimal(str(line_data.quantity)) * Decimal(str(line_data.rate))),
            class_name=line_data.class_name,
            job_id=line_data.job_id,
            class_id=line_data.class_id,
            cost_code_id=line_data.cost_code_id,
            is_taxable=(
                line_data.is_taxable if line_data.is_taxable is not None else True
            ),
            line_order=line_data.line_order or i,
        )
        db.add(line)

    # Drafts are operational work only. Explicit issue posts the ledger and stock.

    db.commit()
    db.refresh(invoice)
    resp = InvoiceResponse.model_validate(invoice)
    resp.customer_name = cust_name
    return resp


@router.put("/{invoice_id}", response_model=InvoiceResponse)
def update_invoice(invoice_id: int, data: InvoiceUpdate, db: Session = Depends(get_db)):
    from app.services.invoice_lifecycle import (
        require_editable_draft,
        stored_invoice_totals,
    )
    from app.services.currency import resolve_rate

    invoice = (
        db.query(Invoice)
        .filter(Invoice.id == invoice_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    require_editable_draft(db, invoice)
    check_closing_date(db, invoice.date)
    update_data = data.model_dump(exclude_unset=True, exclude={"lines"})
    if "status" in update_data and update_data.pop("status") != InvoiceStatus.DRAFT:
        raise HTTPException(
            status_code=409, detail="Use issue or void to change invoice status"
        )
    for field in ("customer_id", "date", "tax_rate"):
        if field in update_data and update_data[field] is None:
            raise HTTPException(status_code=422, detail=f"{field} cannot be null")
    customer_id = update_data.get("customer_id", invoice.customer_id)
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    if "currency" in update_data or "exchange_rate" in update_data:
        currency, rate = resolve_rate(
            db,
            update_data.get("currency", invoice.currency),
            update_data.get("exchange_rate", invoice.exchange_rate),
        )
        update_data.update(currency=currency, exchange_rate=rate)
    for key, value in update_data.items():
        if key == "is_pledge" and value is None:
            continue
        setattr(invoice, key, value)
    check_closing_date(db, invoice.date)
    if invoice.due_date is None:
        invoice.due_date = _due_date_from_terms(invoice.date, invoice.terms)

    if data.lines is not None:
        if not data.lines:
            raise HTTPException(
                status_code=422, detail="Invoice needs at least one line"
            )
        resolve_line_taxable(db, data.lines, customer)
        # Clear the relationship as well as rows so stale in-session lines cannot
        # survive an edit and reach issue. Preserve job/class/cost-code dimensions.
        invoice.lines.clear()
        db.flush()
        for i, line in enumerate(data.lines):
            values = line.model_dump()
            values["amount"] = _q(line.quantity * line.rate)
            values["line_order"] = line.line_order or i
            invoice.lines.append(InvoiceLine(**values))
    invoice.subtotal, invoice.tax_amount, invoice.total = stored_invoice_totals(invoice)
    invoice.balance_due = Decimal("0")
    _check_fair_value(invoice.fair_value_amount, invoice.total)
    db.commit()
    db.refresh(invoice)
    resp = InvoiceResponse.model_validate(invoice)
    resp.customer_name = customer.name
    return resp
