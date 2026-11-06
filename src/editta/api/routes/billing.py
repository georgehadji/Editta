"""Stripe Checkout redirect + webhook endpoint.

The webhook route opens its own tenant-scoped session (via UnitOfWork) only after
verifying the signature and extracting tenant_id from the event — that's the
earliest point tenant is known, so it can't go through the usual get_tenant_session
dependency chain, which requires an authenticated request. Stripe isn't logged in.
"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from editta.adapters.persistence.repositories import CreditLedgerRepository
from editta.adapters.persistence.uow import UnitOfWork
from editta.api.deps import AuthContext, get_billing_service, get_current_auth, get_uow
from editta.services.billing import BillingService, WebhookVerificationError

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/checkout")
async def start_checkout(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    billing: Annotated[BillingService, Depends(get_billing_service)],
) -> RedirectResponse:
    # The Stripe SDK call is synchronous: off the event loop, so a slow Stripe cannot
    # stall every other request on this process.
    url = await asyncio.to_thread(
        billing.create_checkout_session_url, tenant_id=auth.tenant_id, customer_email=auth.email
    )
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    billing: Annotated[BillingService, Depends(get_billing_service)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
) -> dict[str, bool]:
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = billing.verify_webhook_event(payload=payload, signature=signature)
    except WebhookVerificationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    purchase = billing.extract_purchase(event)
    if purchase is not None:
        async with uow.begin(purchase.tenant_id) as session:
            # Stripe redelivers webhooks; append_once keys on the event id, so a
            # replay is a no-op (ON CONFLICT DO NOTHING) rather than a double credit
            # or an aborted transaction.
            await CreditLedgerRepository(session).append_once(
                tenant_id=purchase.tenant_id,
                delta=purchase.credits,
                reason="purchase",
                idempotency_key=f"stripe:{event['id']}",
                stripe_ref=purchase.checkout_session_id,
            )
    return {"received": True}


@router.get("/success", response_class=HTMLResponse)
async def checkout_success() -> HTMLResponse:
    return HTMLResponse("<h1>Payment received</h1><p>Your credits will appear shortly.</p>")


@router.get("/cancel", response_class=HTMLResponse)
async def checkout_cancel() -> HTMLResponse:
    return HTMLResponse("<h1>Checkout canceled</h1>")
