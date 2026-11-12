"""Stripe Checkout session creation + webhook verification.

Pure Stripe logic only — no DB access. The webhook route (api/routes/billing.py)
opens the tenant-scoped session itself once extract_purchase() reveals which
tenant paid, since that isn't known until after the signature is verified.
Webhook replay-safety comes from the same append-only ledger idempotency_key
UNIQUE constraint used everywhere else (PLAN.md §3): the Stripe event id IS the
idempotency key, so a redelivered webhook is a no-op, not a double credit.
"""

from dataclasses import dataclass
from typing import Final
from uuid import UUID

import stripe
import structlog

from editta.domain.errors import EdittaError

logger = structlog.get_logger(__name__)

CHECKOUT_COMPLETED_EVENT: Final = "checkout.session.completed"
# Delayed payment methods (SEPA debit, bank transfer) complete the checkout while the
# payment is still "unpaid"; Stripe confirms the money later with this event.
ASYNC_PAYMENT_SUCCEEDED_EVENT: Final = "checkout.session.async_payment_succeeded"
# "no_payment_required" is a checkout fully covered by a coupon.
_SETTLED: Final = frozenset({"paid", "no_payment_required"})


@dataclass(frozen=True, slots=True)
class Purchase:
    """Credits owed for one settled Checkout Session."""

    tenant_id: UUID
    credits: int
    checkout_session_id: str


class WebhookVerificationError(EdittaError):
    pass


class BillingService:
    def __init__(
        self,
        *,
        client: stripe.StripeClient,
        webhook_secret: str,
        price_id: str,
        credits_per_purchase: int,
        success_url: str,
        cancel_url: str,
    ) -> None:
        self._client = client
        self._webhook_secret = webhook_secret
        self._price_id = price_id
        self._credits_per_purchase = credits_per_purchase
        self._success_url = success_url
        self._cancel_url = cancel_url

    def create_checkout_session_url(self, *, tenant_id: UUID, customer_email: str) -> str:
        session = self._client.v1.checkout.sessions.create(
            params={
                "mode": "payment",
                "line_items": [{"price": self._price_id, "quantity": 1}],
                "customer_email": customer_email,
                "success_url": self._success_url,
                "cancel_url": self._cancel_url,
                "metadata": {"tenant_id": str(tenant_id)},
            }
        )
        return str(session.url)

    def verify_webhook_event(self, *, payload: bytes, signature: str) -> stripe.Event:
        try:
            return self._client.construct_event(payload, signature, self._webhook_secret)
        except (ValueError, stripe.error.SignatureVerificationError) as exc:
            raise WebhookVerificationError("invalid Stripe webhook signature") from exc

    def extract_purchase(self, event: stripe.Event) -> Purchase | None:
        """The purchase to credit, or None when this event settles nothing: another event
        type, a checkout whose payment is still pending, or a session that is not ours.

        Stripe's fulfillment guide says fulfillment may run "multiple times with the
        same Checkout Session ID" and that two separate events can arrive for one
        session, so the caller credits once per session id, not per event id
        (docs/STATE_MACHINES_PLAN.md, D-3).
        """
        if event["type"] not in (CHECKOUT_COMPLETED_EVENT, ASYNC_PAYMENT_SUCCEEDED_EVENT):
            return None
        session = event["data"]["object"]
        if event["type"] == CHECKOUT_COMPLETED_EVENT and session["payment_status"] not in _SETTLED:
            return None
        try:
            tenant_id = UUID(session["metadata"]["tenant_id"])
        except (KeyError, TypeError, ValueError):
            # Another integration on the same Stripe account (a Payment Link, say).
            # Answering 5xx would make Stripe retry for days.
            logger.warning("billing.event_without_tenant", event_id=event["id"])
            return None
        return Purchase(tenant_id, self._credits_per_purchase, str(session["id"]))
