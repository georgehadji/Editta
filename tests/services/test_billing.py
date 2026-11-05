from types import SimpleNamespace
from uuid import uuid4

import pytest
import stripe

from editta.services.billing import BillingService, Purchase, WebhookVerificationError

WEBHOOK_SECRET = "whsec_test"  # noqa: S105 -- test fixture, not a real secret


class _FakeCheckoutSessions:
    def __init__(self, url: str) -> None:
        self.url = url
        self.last_params: dict[str, object] | None = None

    def create(self, *, params: dict[str, object]) -> SimpleNamespace:
        self.last_params = params
        return SimpleNamespace(url=self.url)


class _FakeStripeClient:
    def __init__(
        self, event: dict[str, object] | None = None, *, raise_invalid: bool = False
    ) -> None:
        self.v1 = SimpleNamespace(
            checkout=SimpleNamespace(
                sessions=_FakeCheckoutSessions("https://checkout.stripe.test/abc")
            )
        )
        self._event = event
        self._raise_invalid = raise_invalid

    def construct_event(self, payload: bytes, signature: str, secret: str) -> dict[str, object]:
        if self._raise_invalid:
            raise stripe.error.SignatureVerificationError("bad signature", signature)
        assert self._event is not None
        return self._event


def _checkout_event(
    event_id: str,
    tenant_id: str | None,
    *,
    event_type: str = "checkout.session.completed",
    payment_status: str = "paid",
) -> dict[str, object]:
    return {
        "id": event_id,
        "type": event_type,
        "data": {
            "object": {
                "id": "cs_test_1",
                "payment_status": payment_status,
                "metadata": {} if tenant_id is None else {"tenant_id": tenant_id},
            }
        },
    }


def _service(client: _FakeStripeClient) -> BillingService:
    return BillingService(
        client=client,  # type: ignore[arg-type]
        webhook_secret=WEBHOOK_SECRET,
        price_id="price_test",
        credits_per_purchase=10,
        success_url="https://app.test/success",
        cancel_url="https://app.test/cancel",
    )


def test_create_checkout_session_url_passes_tenant_metadata() -> None:
    client = _FakeStripeClient()
    service = _service(client)
    tenant_id = uuid4()

    url = service.create_checkout_session_url(tenant_id=tenant_id, customer_email="e@x.com")

    assert url == "https://checkout.stripe.test/abc"
    assert client.v1.checkout.sessions.last_params["metadata"] == {"tenant_id": str(tenant_id)}


def test_extract_purchase_on_checkout_completed() -> None:
    tenant_id = uuid4()
    service = _service(_FakeStripeClient())

    result = service.extract_purchase(_checkout_event("evt_1", str(tenant_id)))

    assert result == Purchase(tenant_id, 10, "cs_test_1")


@pytest.mark.parametrize(
    ("event_type", "payment_status", "credited"),
    [
        ("checkout.session.completed", "paid", True),
        ("checkout.session.completed", "no_payment_required", True),
        ("checkout.session.completed", "unpaid", False),  # SEPA etc.: money not here yet
        ("checkout.session.async_payment_succeeded", "paid", True),
        ("checkout.session.async_payment_failed", "unpaid", False),
    ],
)
def test_credits_only_for_settled_payments(
    event_type: str, payment_status: str, credited: bool
) -> None:
    tenant_id = uuid4()
    event = _checkout_event(
        "evt_1", str(tenant_id), event_type=event_type, payment_status=payment_status
    )

    purchase = _service(_FakeStripeClient()).extract_purchase(event)  # type: ignore[arg-type]

    assert (purchase is not None) is credited


@pytest.mark.parametrize("tenant_id", [None, "not-a-uuid"])
def test_checkout_not_made_by_us_is_ignored_not_an_error(tenant_id: str | None) -> None:
    event = _checkout_event("evt_1", tenant_id)

    assert _service(_FakeStripeClient()).extract_purchase(event) is None  # type: ignore[arg-type]


def test_extract_purchase_ignores_unrelated_event_types() -> None:
    service = _service(_FakeStripeClient())
    event = {"id": "evt_2", "type": "customer.created", "data": {"object": {}}}

    assert service.extract_purchase(event) is None


def test_verify_webhook_event_returns_parsed_event() -> None:
    event = _checkout_event("evt_1", str(uuid4()))
    service = _service(_FakeStripeClient(event))

    verified = service.verify_webhook_event(payload=b"{}", signature="sig")

    assert verified == event


def test_verify_webhook_event_invalid_signature_raises() -> None:
    service = _service(_FakeStripeClient(raise_invalid=True))

    with pytest.raises(WebhookVerificationError):
        service.verify_webhook_event(payload=b"{}", signature="bad-sig")
