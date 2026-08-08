"""Stripe Checkout session creation + webhook verification.

Pure Stripe logic only — no DB access. The webhook route (api/routes/billing.py)
opens the tenant-scoped session itself once extract_purchase() reveals which
tenant paid, since that isn't known until after the signature is verified.
Webhook replay-safety comes from the same append-only ledger idempotency_key
UNIQUE constraint used everywhere else (PLAN.md §3): the Stripe event id IS the
idempotency key, so a redelivered webhook is a no-op, not a double credit.
"""

from uuid import UUID

import stripe

from editta.domain.errors import EdittaError

CHECKOUT_COMPLETED_EVENT = "checkout.session.completed"


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

    def extract_purchase(self, event: stripe.Event) -> tuple[UUID, int, str] | None:
        """Returns (tenant_id, credits_to_award, stripe_object_id), or None if this
        event isn't a completed checkout (nothing to credit)."""
        if event["type"] != CHECKOUT_COMPLETED_EVENT:
            return None
        session = event["data"]["object"]
        tenant_id = UUID(session["metadata"]["tenant_id"])
        return tenant_id, self._credits_per_purchase, str(session["id"])
