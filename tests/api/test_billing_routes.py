"""The Stripe webhook route: credits are granted once, and only for settled payments."""

import json
from collections.abc import Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from editta.api import deps
from editta.api.main import app
from editta.api.routes import billing as billing_routes
from editta.services.billing import BillingService

TENANT_ID = uuid4()
UNKNOWN_TENANTS: set[UUID] = set()
# Purchases credited before the session-id key, by event id: idempotency_key -> stripe_ref.
LEGACY_REFS: dict[str, str] = {}


class _EchoStripeClient:
    """Treats the posted body as an already-verified event."""

    def construct_event(self, payload: bytes, signature: str, secret: str) -> dict[str, Any]:
        event: dict[str, Any] = json.loads(payload)
        return event


@pytest.fixture
def ledger() -> Iterator[dict[str, int]]:
    """idempotency_key -> delta, with the real ledger's append-once semantics."""
    entries: dict[str, int] = {}
    refs: dict[str, str] = {}  # idempotency_key -> stripe_ref

    class FakeLedger:
        def __init__(self, session: object) -> None: ...

        async def has_purchase(self, stripe_ref: str) -> bool:
            return stripe_ref in refs.values() or stripe_ref in LEGACY_REFS.values()

        async def append_once(
            self, *, delta: int, idempotency_key: str, stripe_ref: str | None = None, **_: object
        ) -> None:
            entries.setdefault(idempotency_key, delta)
            if stripe_ref is not None:
                refs.setdefault(idempotency_key, stripe_ref)

    class FakeTenants:
        def __init__(self, session: object) -> None: ...

        async def get_by_id(self, tenant_id: UUID) -> object | None:
            return None if tenant_id in UNKNOWN_TENANTS else object()

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
    original_ledger = billing_routes.CreditLedgerRepository
    original_tenants = billing_routes.TenantRepository
    billing_routes.CreditLedgerRepository = FakeLedger  # type: ignore[misc,assignment]
    billing_routes.TenantRepository = FakeTenants  # type: ignore[misc,assignment]
    UNKNOWN_TENANTS.clear()
    LEGACY_REFS.clear()
    yield entries
    billing_routes.CreditLedgerRepository = original_ledger  # type: ignore[misc]
    billing_routes.TenantRepository = original_tenants  # type: ignore[misc]
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

    assert ledger == {"stripe-session:cs_1": 10}


def test_two_crediting_events_for_one_checkout_credit_once(ledger: dict[str, int]) -> None:
    """Stripe may send `completed` (paid) and `async_payment_succeeded` for one session."""
    with TestClient(app) as client:
        for event in (
            _event("evt_5", "checkout.session.completed", "paid"),
            _event("evt_6", "checkout.session.async_payment_succeeded", "paid"),
        ):
            response = client.post(
                "/billing/webhook", json=event, headers={"stripe-signature": "s"}
            )
            assert response.status_code == 200

    assert ledger == {"stripe-session:cs_1": 10}


def test_a_session_credited_under_the_old_event_key_is_not_credited_again(
    ledger: dict[str, int],
) -> None:
    LEGACY_REFS["stripe:evt_7"] = "cs_1"

    with TestClient(app) as client:
        response = client.post(
            "/billing/webhook",
            json=_event("evt_8", "checkout.session.async_payment_succeeded", "paid"),
            headers={"stripe-signature": "s"},
        )

    assert response.status_code == 200
    assert ledger == {}


def test_a_purchase_for_a_tenant_we_do_not_have_is_acknowledged_not_a_500(
    ledger: dict[str, int],
) -> None:
    """Another environment on the same Stripe account: a 500 would make Stripe retry for days."""
    UNKNOWN_TENANTS.add(TENANT_ID)

    with TestClient(app) as client:
        response = client.post(
            "/billing/webhook",
            json=_event("evt_4", "checkout.session.completed", "paid"),
            headers={"stripe-signature": "s"},
        )

    assert response.status_code == 200
    assert ledger == {}


def test_checkout_without_our_metadata_is_acknowledged_not_a_500(ledger: dict[str, int]) -> None:
    event = _event("evt_3", "checkout.session.completed", "paid")
    event["data"]["object"]["metadata"] = {}

    with TestClient(app) as client:
        response = client.post("/billing/webhook", json=event, headers={"stripe-signature": "s"})

    assert response.status_code == 200
    assert ledger == {}
