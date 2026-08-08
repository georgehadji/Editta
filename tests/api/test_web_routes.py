"""Web UI contract tests: the login -> email -> dashboard -> upload -> billing
flow PLAN.md §Phase 3 calls "a non-technical editor completes upload -> pay ->
report unassisted". dependency_overrides swap in the same fakes as
tests/api/test_routes_contract.py, so no Postgres/S3/Stripe/SMTP is needed.
"""

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
)
from editta.api.main import app
from editta.services.accounts import AccountService
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


@pytest.fixture
def client():  # noqa: ANN201
    ledger = FakeLedgerRepo()
    account_service = AccountService(
        tenants=FakeTenantRepo(),  # type: ignore[arg-type]
        users=FakeUserRepo(),  # type: ignore[arg-type]
        session_secret=TEST_SESSION_SECRET,
    )

    app.dependency_overrides[get_manuscript_repo] = lambda: FakeManuscriptRepo()
    app.dependency_overrides[get_job_repo] = lambda: FakeAnalysisJobRepo()
    app.dependency_overrides[get_report_repo] = lambda: FakeReportRepo()
    app.dependency_overrides[get_audit_repo] = lambda: FakeAuditRepo()
    app.dependency_overrides[get_blob_store] = lambda: FakeBlobStore()
    app.dependency_overrides[get_ledger_repo] = lambda: ledger
    app.dependency_overrides[get_account_service] = lambda: account_service
    app.dependency_overrides[get_email_sender] = lambda: ConsoleEmailSender()
    app.dependency_overrides[get_billing_service] = lambda: _FakeBillingService()

    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client

    app.dependency_overrides.clear()


def test_login_page_renders(client: TestClient) -> None:
    tenant_id = uuid4()
    response = client.get(f"/t/{tenant_id}/login")
    assert response.status_code == 200
    assert "Sign in" in response.text


def test_full_login_upload_flow_unassisted(client: TestClient) -> None:
    tenant_id = uuid4()

    check_email = client.post(f"/t/{tenant_id}/login", data={"email": "editor@example.com"})
    assert check_email.status_code == 200
    assert "editor@example.com" in check_email.text
    # ConsoleEmailSender surfaces the link directly since no SMTP is configured in tests.
    assert "auth/click?token=" in check_email.text
    link_start = check_email.text.index("/t/", check_email.text.index("auth/click") - 40)
    link_end = check_email.text.index('"', link_start)
    click_path = check_email.text[link_start:link_end]

    dashboard = client.get(click_path, follow_redirects=True)
    assert dashboard.status_code == 200
    assert "0 credits" in dashboard.text

    csrf = client.cookies.get("editta_csrf")
    upload = client.post(
        f"/t/{tenant_id}/upload",
        files={"file": ("essay.docx", _docx_bytes())},
        headers={"X-CSRF-Token": csrf},
    )
    assert upload.status_code == 200
    assert "essay.docx" not in upload.text  # job list shows manuscript_id, not filename
    assert "pending" in upload.text


def test_billing_checkout_redirects_to_stripe(client: TestClient) -> None:
    tenant_id = uuid4()
    check_email = client.post(f"/t/{tenant_id}/login", data={"email": "editor@example.com"})
    link_start = check_email.text.index("/t/", check_email.text.index("auth/click") - 40)
    link_end = check_email.text.index('"', link_start)
    client.get(check_email.text[link_start:link_end], follow_redirects=True)

    response = client.get("/billing/checkout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "https://checkout.stripe.test/fake"


def test_dashboard_without_login_redirects_or_401s(client: TestClient) -> None:
    response = client.get(f"/t/{uuid4()}/dashboard")
    assert response.status_code == 401
