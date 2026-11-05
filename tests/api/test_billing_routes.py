"""The Stripe webhook route: credits are granted once, and only for settled payments."""

import json
from collections.abc import Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from editta.api import deps
from editta.api.main import app
from editta.api.routes import billing as billing_routes
from editta.services.billing import BillingService

TENANT_ID = uuid4()


class _EchoStripeClient:
    """Treats the posted body as an already-verified event."""

    def construct_event(self, payload: bytes, signature: str, secret: str) -> dict[str, Any]:
        event: dict[str, Any] = json.loads(payload)
        return event


@pytest.fixture
def ledger() -> Iterator[dict[str, int]]:
    """idempotency_key -> delta, with the real ledger's append-once semantics."""
    entries: dict[str, int] = {}

    class FakeLedger:
        def __init__(self, session: object) -> None: ...

        async def append_once(self, *, delta: int, idempotency_key: str, **_: object) -> None:
            entries.setdefault(idempotency_key, delta)

    @asynccontextmanager
    async def begin(tenant_id: object):  # noqa: ANN202
        yield object()

    billing = BillingService(
        client=_EchoStripeClient(),  # type: ignore[arg-type]
        webhook_secret="whsec_test",  # noqa: S106 -- test fixture
        price_id="price_test",
        credits_per_purchase=10,
        success_url="https://app.test/success",
        cancel_url="https://app.test/cancel",
    )
    app.dependency_overrides[deps.get_billing_service] = lambda: billing
    app.dependency_overrides[deps.get_uow] = lambda: SimpleNamespace(begin=begin)
    original = billing_routes.CreditLedgerRepository
    billing_routes.CreditLedgerRepository = FakeLedger  # type: ignore[misc,assignment]
    yield entries
    billing_routes.CreditLedgerRepository = original  # type: ignore[misc]
    app.dependency_overrides.clear()


def _event(event_id: str, event_type: str, payment_status: str) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": event_type,
        "data": {
            "object": {
                "id": "cs_1",
                "payment_status": payment_status,
                "metadata": {"tenant_id": str(TENANT_ID)},
            }
        },
    }


def test_delayed_payment_credits_once_when_the_money_arrives(ledger: dict[str, int]) -> None:
    with TestClient(app) as client:
        # SEPA debit: checkout completes unpaid, then Stripe confirms the payment.
        for event in (
            _event("evt_1", "checkout.session.completed", "unpaid"),
            _event("evt_2", "checkout.session.async_payment_succeeded", "paid"),
            _event("evt_2", "checkout.session.async_payment_succeeded", "paid"),  # redelivery
        ):
            response = client.post(
                "/billing/webhook", json=event, headers={"stripe-signature": "s"}
            )
            assert response.status_code == 200

    assert ledger == {"stripe:evt_2": 10}


def test_checkout_without_our_metadata_is_acknowledged_not_a_500(ledger: dict[str, int]) -> None:
    event = _event("evt_3", "checkout.session.completed", "paid")
    event["data"]["object"]["metadata"] = {}

    with TestClient(app) as client:
        response = client.post("/billing/webhook", json=event, headers={"stripe-signature": "s"})

    assert response.status_code == 200
    assert ledger == {}
