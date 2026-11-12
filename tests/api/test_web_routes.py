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
from editta.config import Settings
from editta.domain.job_view import job_view
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
    assert "essay.docx" in upload.text  # the job list names the file, not its id
    assert "Queued" in upload.text


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
    after = client.get(f"/t/{TENANT_ID}/dashboard", follow_redirects=False)
    assert after.status_code == 303


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


def test_dashboard_without_login_sends_the_browser_to_sign_in(client: TestClient) -> None:
    tenant = uuid4()
    response = client.get(f"/t/{tenant}/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/t/{tenant}/login"


def test_an_expired_session_during_polling_sends_htmx_to_sign_in(client: TestClient) -> None:
    """htmx does not swap a 401, so the poll used to freeze silently."""
    response = client.get(f"/t/{TENANT_ID}/jobs-fragment", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == f"/t/{TENANT_ID}/login"


def test_other_htmx_errors_come_back_as_text_for_the_alert_region(client: TestClient) -> None:
    _sign_in(client)
    response = client.post(
        f"/t/{TENANT_ID}/upload",
        files={"file": ("essay.docx", _docx_bytes())},
        headers={"X-CSRF-Token": "wrong", "HX-Request": "true"},
    )
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert "{" not in response.text  # not FastAPI's JSON body


def test_the_json_api_keeps_its_401_body(client: TestClient) -> None:
    response = client.get(f"/manuscripts/jobs/{uuid4()}", follow_redirects=False)
    assert response.status_code == 401
    assert "detail" in response.json()


def test_every_page_routes_htmx_errors_to_the_alert_region(client: TestClient) -> None:
    page = client.get(f"/t/{TENANT_ID}/login").text
    assert 'id="htmx-error"' in page
    assert '"target": "#htmx-error"' in page


def test_an_unusable_sign_in_link_shows_the_form_with_the_reason(client: TestClient) -> None:
    link = _emailed_link(client)
    assert _confirm(client, link).status_code == 200
    client.cookies.clear()

    page = client.get(link)
    assert page.status_code == 401
    assert "already used" in page.text
    assert f'action="/t/{TENANT_ID}/login"' in page.text  # ask for a new link right here


def test_a_refused_confirm_shows_the_sign_in_page(client: TestClient) -> None:
    token = _emailed_link(client).split("token=", 1)[1]
    page = client.post(f"/t/{TENANT_ID}/auth/click", data={"token": token, "nonce": "guessed"})
    assert page.status_code == 403
    assert "open the sign-in link in this browser" in page.text
    assert f'action="/t/{TENANT_ID}/login"' in page.text


def test_the_upload_form_shows_it_is_busy_and_cannot_be_sent_twice(client: TestClient) -> None:
    _sign_in(client)
    page = client.get(f"/t/{TENANT_ID}/dashboard").text
    assert 'hx-disabled-elt="find button"' in page
    assert 'id="upload-busy"' in page
    assert 'id="job-summary"' in page and 'aria-live="polite"' in page


def test_a_successful_upload_clears_old_errors_and_the_file_field(client: TestClient) -> None:
    _sign_in(client)
    upload = client.post(
        f"/t/{TENANT_ID}/upload",
        files={"file": ("essay.docx", _docx_bytes())},
        headers={"X-CSRF-Token": client.cookies.get("editta_csrf"), "HX-Request": "true"},
    )
    assert '<div id="upload-error" hx-swap-oob="true"></div>' in upload.text
    assert 'id="htmx-error" class="error-text" role="alert" hx-swap-oob="true"' in upload.text
    assert 'id="file"' in upload.text and 'hx-swap-oob="true"' in upload.text
    assert "1 in progress" in upload.text  # the screen-reader summary


def test_a_poll_updates_the_summary_but_leaves_errors_alone(client: TestClient) -> None:
    _sign_in(client)
    poll = client.get(f"/t/{TENANT_ID}/jobs-fragment", headers={"HX-Request": "true"})
    assert 'id="job-summary"' in poll.text
    assert "upload-error" not in poll.text
    assert 'id="htmx-error"' not in poll.text  # an upload's error stays until the next upload
    # ...but a good poll clears the message an earlier failed poll left.
    assert '<div id="poll-error" class="error-text" role="alert" hx-swap-oob="true">' in poll.text


def test_a_failed_poll_puts_its_message_where_the_next_poll_clears_it(
    client: TestClient,
) -> None:
    _sign_in(client)
    other_tenant = "00000000-0000-0000-0000-000000000000"
    poll = client.get(f"/t/{other_tenant}/jobs-fragment", headers={"HX-Request": "true"})
    assert poll.status_code >= 400
    assert poll.headers["HX-Retarget"] == "#poll-error"


def test_checkout_success_links_back_to_the_dashboard(client: TestClient) -> None:
    anonymous = client.get("/billing/success")
    assert anonymous.status_code == 200
    assert "dashboard" not in anonymous.text

    _sign_in(client)
    page = client.get("/billing/success")
    assert f'href="/t/{TENANT_ID}/dashboard"' in page.text


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


def _rendered_job_list(
    *, state: str, kinds: set[str], error: str | None = None, failure_kind: str | None = None
) -> str:
    job = SimpleNamespace(id=uuid4(), manuscript_id=uuid4())
    template = get_templates().get_template("_job_list.html")
    return template.render(
        tenant_id=TENANT_ID,
        jobs=[job],
        views={job.id: job_view(state, error, failure_kind, kinds)},
        titles={job.manuscript_id: "Ο λαβύρινθος.docx"},
    )


def test_each_job_is_listed_by_its_file_name() -> None:
    assert "Ο λαβύρινθος.docx" in _rendered_job_list(state="analyzing", kinds=set())


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
    assert "Running: analyzing" in html
    assert "/artifacts/report" not in html
    assert "annotated_manuscript" not in html


def test_a_job_waiting_to_retry_says_so_instead_of_looking_failed() -> None:
    html = _rendered_job_list(state="pending", kinds=set(), error="the model timed out")
    assert "Retrying" in html
    assert 'class="note-text">the model timed out' in html
    assert "error-text" not in html


def test_a_failed_upload_says_the_credit_came_back_and_what_to_do() -> None:
    html = _rendered_job_list(
        state="dead", kinds=set(), error="not a readable .docx", failure_kind="customer"
    )
    assert "Failed — credit refunded" in html
    assert 'class="error-text">not a readable .docx' in html
    assert "Fix the file and upload it again." in html
    assert "/artifacts/report" not in html


def test_a_failure_on_our_side_says_so() -> None:
    html = _rendered_job_list(state="dead", kinds=set(), error="gave up after 3 attempts")
    assert "Failed — credit refunded" in html
    assert "on our side" in html


def test_a_report_deleted_by_retention_offers_no_dead_link() -> None:
    html = _rendered_job_list(state="completed", kinds=set())
    assert "Expired" in html
    assert "/artifacts/report" not in html


def test_an_unknown_state_renders_instead_of_crashing() -> None:
    assert "Unknown" in _rendered_job_list(state="awaiting_batch", kinds=set())


def test_an_empty_list_says_nothing_was_uploaded() -> None:
    template = get_templates().get_template("_job_list.html")
    html = template.render(tenant_id=TENANT_ID, jobs=[], views={}, titles={}, polling=False)
    assert "No manuscripts uploaded yet." in html


def test_the_job_table_asks_for_itself_only_while_a_job_is_running() -> None:
    template = get_templates().get_template("_job_list.html")
    context = {"tenant_id": TENANT_ID, "jobs": [], "views": {}, "titles": {}}

    assert "hx-trigger" in template.render(**context, polling=True)
    assert "hx-trigger" not in template.render(**context, polling=False)
