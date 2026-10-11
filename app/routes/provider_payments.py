# ============================================================================
# Provider payments — generic checkout/webhook/poll routes for every online
# payment provider (Stripe today; PayPal/Square plug into the registry).
#
# Replaces app/routes/stripe_payments.py. The /api/stripe/webhook path is
# kept as an alias so operator webhook configs in the Stripe dashboard
# keep working.
#
# Auth model:
#   - create-checkout-session: UNAUTHENTICATED (called from the public pay
#     page) but rate-limited; the payment_token IS the capability.
#   - webhook: UNAUTHENTICATED; the provider signature is the auth. A
#     payload that fails verification is a 400 and records nothing.
#   - check-status: authenticated (SPA) — the desktop-mode fallback since
#     webhooks can't reach 127.0.0.1.
# ============================================================================

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from app.schemas.common import StrictModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.invoices import Invoice, InvoiceStatus
from app.services.payments import get_provider, provider_settings
from app.services.payments.recorder import record_provider_payment
from app.services.rate_limit import limiter


class CheckoutSessionRequest(StrictModel):
    payment_token: str


router = APIRouter(prefix="/api/payments", tags=["payments"])


def _resolve_provider(name: str):
    try:
        return get_provider(name)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unknown payment provider: {name}")


def _require_provider(db: Session, name: str):
    """Resolve provider + settings; 400 unless enabled and configured."""
    provider = _resolve_provider(name)
    settings = provider_settings(db, provider)
    if not provider.is_enabled(settings):
        raise HTTPException(status_code=400, detail="Online payments are not enabled")
    if not provider.is_configured(settings):
        raise HTTPException(
            status_code=400,
            detail=f"{provider.display_name} is not configured",
        )
    return provider, settings


@router.post("/{provider_name}/create-checkout-session")
@limiter.limit("10/minute")
def create_checkout(
    provider_name: str,
    data: CheckoutSessionRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Create a hosted checkout for an invoice (public; token = capability)."""
    provider, settings = _require_provider(db, provider_name)

    invoice = (
        db.query(Invoice).filter(Invoice.payment_token == data.payment_token).first()
    )
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    from app.services.invoice_lifecycle import require_issued

    require_issued(invoice)
    if invoice.status in (InvoiceStatus.PAID, InvoiceStatus.VOID):
        raise HTTPException(status_code=400, detail="Invoice is already paid or void")
    if invoice.balance_due <= 0:
        raise HTTPException(status_code=400, detail="No balance due")
    from app.services.currency import document_currency, home_currency

    if document_currency(invoice, db) != home_currency(db):
        raise HTTPException(
            status_code=400,
            detail=(
                "Online checkout is only available for home-currency invoices; "
                "record this payment manually"
            ),
        )

    base_url = str(request.base_url).rstrip("/")
    checkout = provider.create_checkout(invoice, settings, base_url)

    invoice.checkout_provider = provider.name
    invoice.checkout_external_id = checkout.external_id
    if provider.name == "stripe":
        # Kept mirrored during the transition off the provider-specific column
        invoice.stripe_checkout_session_id = checkout.external_id
    db.commit()

    return {"checkout_url": checkout.url}


@router.post("/{provider_name}/webhook")
async def provider_webhook(
    provider_name: str, request: Request, db: Session = Depends(get_db)
):
    """Handle a provider webhook. Signature verification IS the auth."""
    provider = _resolve_provider(provider_name)
    settings = provider_settings(db, provider)

    payload = await request.body()
    try:
        result = provider.verify_webhook(payload, request.headers, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if result is None:
        return {"status": "ignored"}
    if result.status != "paid" or result.amount is None:
        return {"status": "ignored"}

    invoice_id = result.invoice_id
    if invoice_id is None:
        # Providers without metadata passthrough resolve the invoice by the
        # stored checkout id.
        invoice = (
            db.query(Invoice)
            .filter(Invoice.checkout_external_id == result.external_id)
            .first()
        )
        if not invoice:
            return {"status": "invoice_not_found"}
        invoice_id = invoice.id

    status = record_provider_payment(
        db,
        provider.name,
        provider.display_name,
        invoice_id,
        result.external_id,
        result.amount,
    )
    return {"status": status}


@router.post("/{provider_name}/check-status/{invoice_id}")
def check_status(provider_name: str, invoice_id: int, db: Session = Depends(get_db)):
    """Poll the provider for a checkout's state and record it if paid.

    The desktop-mode fallback: webhooks can't reach 127.0.0.1, so the
    invoice page's "Check payment status" button exercises this instead.
    Idempotent with the webhook path — both key on external_id.
    """
    provider, settings = _require_provider(db, provider_name)

    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    external_id = (
        invoice.checkout_external_id
        if invoice.checkout_provider == provider.name
        else None
    )
    if provider.name == "stripe" and not external_id:
        external_id = invoice.stripe_checkout_session_id
    if not external_id:
        return {"status": "no_checkout"}

    result = provider.poll_status(external_id, settings)
    if result.status == "paid" and result.amount is not None:
        recorded = record_provider_payment(
            db,
            provider.name,
            provider.display_name,
            result.invoice_id or invoice.id,
            result.external_id,
            result.amount,
        )
        return {"status": recorded, "provider_status": result.status}
    return {"status": "not_paid", "provider_status": result.status}


@router.get("/payment-link/{invoice_id}")
def get_payment_link(invoice_id: int, request: Request, db: Session = Depends(get_db)):
    """Get the public payment URL for an invoice (provider-agnostic)."""
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    from app.services.invoice_lifecycle import require_issued

    require_issued(invoice)
    if not invoice.payment_token:
        invoice.payment_token = str(uuid.uuid4())
        db.commit()

    base_url = str(request.base_url).rstrip("/")
    return {"url": f"{base_url}/pay/{invoice.payment_token}"}


# ---------------------------------------------------------------------------
# Legacy alias: operator webhook configs in the Stripe dashboard point at
# /api/stripe/webhook. Keep it working by delegating to the generic handler.
# ---------------------------------------------------------------------------

legacy_stripe_router = APIRouter(prefix="/api/stripe", tags=["payments"])


@legacy_stripe_router.post("/webhook")
async def legacy_stripe_webhook(request: Request, db: Session = Depends(get_db)):
    return await provider_webhook("stripe", request, db)
