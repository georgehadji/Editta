"""Web UI contract tests: the login -> email -> dashboard -> upload -> billing
flow PLAN.md §Phase 3 calls "a non-technical editor completes upload -> pay ->
report unassisted". dependency_overrides swap in the same fakes as
tests/api/test_routes_contract.py, so no Postgres/S3/Stripe/SMTP is needed.
"""

import asyncio
import io
from uuid import uuid4

import docx
import pytest
from fastapi.testclient import TestClient

from editta.adapters.email.console import ConsoleEmailSender
from editta.api.deps import (
    get_account_service,
    get_audit_repo,
    get_billing_service,
    get_blob_store,
    get_email_sender,
    get_job_repo,
    get_ledger_repo,
    get_manuscript_repo,
    get_report_repo,
    get_settings,
    get_tenant_repo,
)
from editta.api.main import app
from editta.config import Settings
from editta.services.billing import BillingService
from tests.services.fakes import (
    FakeAnalysisJobRepo,
    FakeAuditRepo,
    FakeBlobStore,
    FakeLedgerRepo,
    FakeManuscriptRepo,
    FakeReportRepo,
    FakeTenantRepo,
    FakeUserRepo,
    make_account_service,
)

TEST_SESSION_SECRET = "web-contract-test-secret"  # noqa: S105 -- test fixture


def _docx_bytes() -> bytes:
    document = docx.Document()
    document.add_paragraph("a manuscript for web contract testing")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class _FakeBillingService(BillingService):
    def __init__(self) -> None:
        pass  # deliberately skip the real __init__ — no Stripe client needed

    def create_checkout_session_url(self, *, tenant_id, customer_email) -> str:  # type: ignore[no-untyped-def]
        return "https://checkout.stripe.test/fake"


TENANT_ID = uuid4()
EDITOR = "editor@example.com"


def _make_client(environment: str):  # noqa: ANN202
    ledger = FakeLedgerRepo()
    ledger.entries.append({"tenant_id": TENANT_ID, "delta": 3, "reason": "purchase"})
    users = FakeUserRepo()
    asyncio.run(users.create(tenant_id=TENANT_ID, email=EDITOR))
    account_service = make_account_service(users, secret=TEST_SESSION_SECRET)
    settings = Settings(environment=environment)  # other fields come from the test env

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_manuscript_repo] = lambda: FakeManuscriptRepo()
    app.dependency_overrides[get_job_repo] = lambda: FakeAnalysisJobRepo()
    app.dependency_overrides[get_report_repo] = lambda: FakeReportRepo()
    app.dependency_overrides[get_audit_repo] = lambda: FakeAuditRepo()
    app.dependency_overrides[get_blob_store] = lambda: FakeBlobStore()
    app.dependency_overrides[get_tenant_repo] = lambda: FakeTenantRepo()
    app.dependency_overrides[get_ledger_repo] = lambda: ledger
    app.dependency_overrides[get_account_service] = lambda: account_service
    app.dependency_overrides[get_email_sender] = lambda: ConsoleEmailSender()
    app.dependency_overrides[get_billing_service] = lambda: _FakeBillingService()
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def client():  # noqa: ANN201
    with _make_client("development") as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def production_client():  # noqa: ANN201
    with _make_client("production") as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _sign_in(client: TestClient) -> None:
    check_email = client.post(f"/t/{TENANT_ID}/login", data={"email": EDITOR})
    link_start = check_email.text.index("/t/", check_email.text.index("auth/click") - 40)
    link_end = check_email.text.index('"', link_start)
    client.get(check_email.text[link_start:link_end], follow_redirects=True)


def test_login_page_renders(client: TestClient) -> None:
    response = client.get(f"/t/{TENANT_ID}/login")
    assert response.status_code == 200
    assert "Sign in" in response.text


def test_full_login_upload_flow_unassisted(client: TestClient) -> None:
    check_email = client.post(f"/t/{TENANT_ID}/login", data={"email": EDITOR})
    assert check_email.status_code == 200
    assert EDITOR in check_email.text
    # Development + console sender: the link is shown instead of emailed.
    assert "auth/click?token=" in check_email.text
    link_start = check_email.text.index("/t/", check_email.text.index("auth/click") - 40)
    link_end = check_email.text.index('"', link_start)
    click_path = check_email.text[link_start:link_end]

    dashboard = client.get(click_path, follow_redirects=True)
    assert dashboard.status_code == 200
    assert "3 credits" in dashboard.text

    csrf = client.cookies.get("editta_csrf")
    upload = client.post(
        f"/t/{TENANT_ID}/upload",
        files={"file": ("essay.docx", _docx_bytes())},
        headers={"X-CSRF-Token": csrf},
    )
    assert upload.status_code == 200
    assert "essay.docx" not in upload.text  # job list shows manuscript_id, not filename
    assert "pending" in upload.text


def test_billing_checkout_redirects_to_stripe(client: TestClient) -> None:
    _sign_in(client)

    response = client.get("/billing/checkout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "https://checkout.stripe.test/fake"


def test_login_page_never_shows_the_link_outside_development(
    production_client: TestClient,
) -> None:
    """A link shown on the page signs in whoever typed the email — no inbox needed."""
    page = production_client.post(f"/t/{TENANT_ID}/login", data={"email": EDITOR})

    assert page.status_code == 200
    assert "auth/click" not in page.text


def test_unknown_email_sees_the_same_page_and_gets_no_link(client: TestClient) -> None:
    page = client.post(f"/t/{TENANT_ID}/login", data={"email": "intruder@example.org"})

    assert page.status_code == 200
    assert "auth/click" not in page.text


def test_dashboard_without_login_redirects_or_401s(client: TestClient) -> None:
    response = client.get(f"/t/{uuid4()}/dashboard")
    assert response.status_code == 401
