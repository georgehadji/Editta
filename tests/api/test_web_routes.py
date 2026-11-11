"""Web UI contract tests: the login -> email -> dashboard -> upload -> billing
flow PLAN.md §Phase 3 calls "a non-technical editor completes upload -> pay ->
report unassisted". dependency_overrides swap in the same fakes as
tests/api/test_routes_contract.py, so no Postgres/S3/Stripe/SMTP is needed.
"""

import asyncio
import io
import re
from types import SimpleNamespace
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
    get_templates,
    get_tenant_repo,
)
from editta.api.main import app
from editta.api.routes.web import _any_job_running
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
    ran_on_event_loop: bool | None = None

    def __init__(self) -> None:
        pass  # deliberately skip the real __init__ — no Stripe client needed

    def create_checkout_session_url(self, *, tenant_id, customer_email) -> str:  # type: ignore[no-untyped-def]
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            _FakeBillingService.ran_on_event_loop = False
        else:
            _FakeBillingService.ran_on_event_loop = True
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


def _emailed_link(client: TestClient, email: str = EDITOR) -> str:
    page = client.post(f"/t/{TENANT_ID}/login", data={"email": email})
    start = page.text.index("/t/", page.text.index("auth/click") - 40)
    return page.text[start : page.text.index('"', start)].replace("&amp;", "&")


def _confirm(client: TestClient, link: str):  # noqa: ANN202
    page = client.get(link)
    token = re.search(r'name="token" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    nonce = re.search(r'name="nonce" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    return client.post(
        f"/t/{TENANT_ID}/auth/click",
        data={"token": token, "nonce": nonce},
        follow_redirects=True,
    )


def _sign_in(client: TestClient) -> None:
    assert _confirm(client, _emailed_link(client)).status_code == 200


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

    dashboard = _confirm(client, _emailed_link(client))
    assert dashboard.status_code == 200
    assert "3 credits" in dashboard.text
    assert f"Signed in as <strong>{EDITOR}</strong>" in dashboard.text

    csrf = client.cookies.get("editta_csrf")
    upload = client.post(
        f"/t/{TENANT_ID}/upload",
        files={"file": ("essay.docx", _docx_bytes())},
        headers={"X-CSRF-Token": csrf},
    )
    assert upload.status_code == 200
    assert "essay.docx" not in upload.text  # job list shows manuscript_id, not filename
    assert "pending" in upload.text


def test_opening_the_link_alone_does_not_sign_in(client: TestClient) -> None:
    """Regression: a bare GET signed the browser in, so an attacker could send a victim
    the attacker's own link and swap the victim's session for theirs."""
    page = client.get(_emailed_link(client))

    assert page.status_code == 200
    assert f"Sign in as {EDITOR}" in page.text
    assert client.cookies.get("editta_session") is None


def test_confirming_without_the_nonce_cookie_is_refused(client: TestClient) -> None:
    """What a cross-site auto-submitting form would send: a valid token, but no
    nonce cookie (a third-party page can neither read nor set it)."""
    link = _emailed_link(client)
    token = link.split("token=", 1)[1]

    response = client.post(f"/t/{TENANT_ID}/auth/click", data={"token": token, "nonce": "guessed"})

    assert response.status_code == 403
    assert client.cookies.get("editta_session") is None


def test_a_leftover_session_cookie_does_not_block_signing_in(client: TestClient) -> None:
    """Regression: the sign-in form sends no CSRF header, so anyone with an old
    session cookie got a 403 when asking for a link."""
    response = client.post(
        f"/t/{TENANT_ID}/login",
        data={"email": EDITOR},
        headers={"Cookie": "editta_session=stale-or-revoked"},
    )

    assert response.status_code == 200


def test_sign_out_clears_the_session(client: TestClient) -> None:
    _sign_in(client)

    response = client.post(
        f"/t/{TENANT_ID}/logout",
        headers={"X-CSRF-Token": client.cookies.get("editta_csrf"), "HX-Request": "true"},
    )

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == f"/t/{TENANT_ID}/login"
    assert client.get(f"/t/{TENANT_ID}/dashboard").status_code == 401


def test_refused_upload_is_shown_to_htmx_users(client: TestClient) -> None:
    """htmx ignores 4xx responses, so a 402 used to change nothing on screen."""
    _sign_in(client)
    for _ in range(3):  # spend the 3 seeded credits
        client.post(
            f"/t/{TENANT_ID}/upload",
            files={"file": ("essay.docx", _docx_bytes())},
            headers={"X-CSRF-Token": client.cookies.get("editta_csrf")},
        )

    refused = client.post(
        f"/t/{TENANT_ID}/upload",
        files={"file": ("essay.docx", _docx_bytes())},
        headers={"X-CSRF-Token": client.cookies.get("editta_csrf"), "HX-Request": "true"},
    )

    assert refused.status_code == 200
    assert refused.headers["HX-Retarget"] == "#upload-error"
    assert "not enough credits" in refused.text and "buy credits" in refused.text


def test_billing_checkout_redirects_to_stripe(client: TestClient) -> None:
    _sign_in(client)

    response = client.get("/billing/checkout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "https://checkout.stripe.test/fake"
    # The Stripe SDK call is synchronous: it must not run on (and block) the event loop.
    assert _FakeBillingService.ran_on_event_loop is False


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


def test_a_sign_in_link_cannot_be_used_twice(client: TestClient) -> None:
    link = _emailed_link(client)
    assert _confirm(client, link).status_code == 200
    client.cookies.clear()

    assert client.get(link).status_code == 401  # spent: no confirm page for a used link


def test_sign_in_link_requests_are_rate_limited(client: TestClient) -> None:
    """Each request can email a stranger's inbox: far tighter than the 120/min default."""
    statuses = [
        client.post(f"/t/{TENANT_ID}/login", data={"email": EDITOR}).status_code for _ in range(11)
    ]

    assert statuses == [200] * 10 + [429]


def _rendered_job_list(*, state: str, kinds: set[str]) -> str:
    job = SimpleNamespace(id=uuid4(), manuscript_id=uuid4(), state=state, error=None)
    template = get_templates().get_template("_job_list.html")
    return template.render(tenant_id=TENANT_ID, jobs=[job], artifacts={job.id: kinds})


def test_a_finished_job_with_comments_offers_the_commented_file() -> None:
    html = _rendered_job_list(state="completed", kinds={"report", "annotated_manuscript"})
    assert "/artifacts/report" in html
    assert "/artifacts/annotated_manuscript" in html


def test_a_finished_job_without_comments_offers_only_the_report() -> None:
    html = _rendered_job_list(state="completed", kinds={"report"})
    assert "/artifacts/report" in html
    assert "annotated_manuscript" not in html


def test_an_unfinished_job_offers_no_download() -> None:
    html = _rendered_job_list(state="analyzing", kinds=set())
    assert "/artifacts/report" not in html
    assert "annotated_manuscript" not in html


def test_the_job_table_asks_for_itself_only_while_a_job_is_running() -> None:
    template = get_templates().get_template("_job_list.html")
    context = {"tenant_id": TENANT_ID, "jobs": [], "artifacts": {}}

    assert "hx-trigger" in template.render(**context, polling=True)
    assert "hx-trigger" not in template.render(**context, polling=False)


@pytest.mark.parametrize(
    ("states", "running"),
    [
        (["completed", "dead", "cancelled"], False),
        ([], False),
        (["completed", "analyzing"], True),
        (["failed"], True),  # between a failed attempt and its retry
    ],
)
def test_a_job_is_running_until_it_reaches_a_final_state(states: list[str], running: bool) -> None:
    assert _any_job_running(states) is running
