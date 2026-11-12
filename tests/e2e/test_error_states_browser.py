"""The dashboard's error states, in a real browser (docs/STATE_MACHINES.md SM-4, SM-5).

Half of this behaviour lives in the browser, not the server: htmx's responseHandling
config in base.html, the out-of-band clears in _job_list_response.html, and
static/editta.js. pytest's TestClient cannot run any of that, so this test drives the
real app in headless Chromium, with the same in-memory fakes as tests/api/test_web_routes.py
(no Postgres, S3, Stripe or SMTP).

Locally the test is skipped when Chromium is not installed
(`python -m playwright install chromium`); in CI it must run.
"""

import asyncio
import html
import io
import os
import re
import socket
import threading
import time
from collections.abc import Iterator
from uuid import UUID, uuid4

import docx
import pytest
import uvicorn

from editta.adapters.email.console import ConsoleEmailSender
from editta.api.deps import (
    get_account_service,
    get_audit_repo,
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

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

TENANT_ID = uuid4()
EDITOR = "editor@example.com"
SECRET = "browser-test-session-secret"  # noqa: S105 -- test fixture
WAIT_MS = 8_000  # polls run every 3 s


class CrashableJobs(FakeAnalysisJobRepo):
    """Listing jobs raises while `crash` is set: an unhandled 500 on the poll."""

    crash = False

    async def list_for_tenant(self, tenant_id: UUID, *, limit: int = 50):  # type: ignore[no-untyped-def]
        if self.crash:
            raise RuntimeError("browser test crash")
        return await super().list_for_tenant(tenant_id, limit=limit)


def _docx_bytes() -> bytes:
    document = docx.Document()
    document.add_paragraph("a manuscript for the browser test")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def browser():  # noqa: ANN201
    with sync_api.sync_playwright() as playwright:
        try:
            chromium = playwright.chromium.launch()
        except sync_api.Error:
            if os.environ.get("CI"):
                raise
            pytest.skip("Chromium is not installed: python -m playwright install chromium")
        yield chromium
        chromium.close()


@pytest.fixture
def server() -> Iterator[tuple[str, FakeLedgerRepo, CrashableJobs]]:
    # One instance of each fake for the whole test: the overrides in test_web_routes.py
    # make a fresh one per request, and then an uploaded job would vanish before a poll.
    ledger = FakeLedgerRepo()
    jobs = CrashableJobs()
    manuscripts, reports = FakeManuscriptRepo(), FakeReportRepo()
    users = FakeUserRepo()
    # Playwright's sync API keeps an event loop running on this thread: create the user
    # on another one.
    seed = threading.Thread(
        target=asyncio.run, args=(users.create(tenant_id=TENANT_ID, email=EDITOR),)
    )
    seed.start()
    seed.join()
    accounts = make_account_service(users, secret=SECRET)
    settings = Settings(environment="development")
    overrides = {
        get_settings: lambda: settings,
        get_manuscript_repo: lambda: manuscripts,
        get_job_repo: lambda: jobs,
        get_report_repo: lambda: reports,
        get_audit_repo: FakeAuditRepo,
        get_blob_store: FakeBlobStore,
        get_tenant_repo: FakeTenantRepo,
        get_ledger_repo: lambda: ledger,
        get_account_service: lambda: accounts,
        get_email_sender: ConsoleEmailSender,
    }
    app.dependency_overrides.update(overrides)

    port = _free_port()
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uvicorn_server.started:
        assert time.monotonic() < deadline, "the test server did not start"
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}", ledger, jobs

    uvicorn_server.should_exit = True
    thread.join(timeout=10)
    app.dependency_overrides.clear()


def test_every_dashboard_error_state_shows_and_clears(  # noqa: PLR0915 -- one walk-through
    browser, server: tuple[str, FakeLedgerRepo, CrashableJobs]
) -> None:
    base, ledger, jobs = server
    tenant = f"{base}/t/{TENANT_ID}"
    context = browser.new_context()
    page = context.new_page()
    script_errors: list[str] = []
    page.on("pageerror", lambda exc: script_errors.append(str(exc)))
    page.on(
        "console",
        lambda msg: (
            script_errors.append(msg.text) if "Content Security Policy" in msg.text else None
        ),
    )

    # A sign-in link that can't be used: the reason, above a form to get a new one.
    response = page.goto(f"{tenant}/auth/click?token=bogus")
    assert response is not None and response.status == 401
    expect(page.get_by_role("alert").filter(has_text="can't be used")).to_be_visible()

    # Sign in with the link the development console sender shows on the page.
    page.get_by_label("Email address").fill(EDITOR)
    page.get_by_role("button", name="Send sign-in link").click()
    expect(page.get_by_text("Check your email")).to_be_visible()
    link = re.search(r"/t/[^\"<\s]+auth/click\?token=[^\"<\s]+", page.content())
    assert link is not None
    page.goto(base + html.unescape(link.group(0)))
    page.get_by_role("button", name=f"Sign in as {EDITOR}").click()
    expect(page).to_have_url(f"{tenant}/dashboard")

    upload_error, htmx_error = page.locator("#upload-error"), page.locator("#htmx-error")
    poll_error, balance = page.locator("#poll-error"), page.locator("#credit-balance")
    button = page.get_by_role("button", name="Upload & analyze")

    def upload(name: str, data: bytes) -> None:
        page.locator("input[type=file]").set_input_files(
            {"name": name, "mimeType": "application/octet-stream", "buffer": data}
        )
        button.click()

    # While an upload is in flight the button is disabled and says so; then it comes back.
    def slow(route) -> None:  # type: ignore[no-untyped-def]
        time.sleep(0.5)
        route.continue_()

    page.route("**/upload", slow)
    upload("bad.docx", b"not a docx")
    expect(button).to_be_disabled()
    expect(page.locator("#upload-busy")).to_be_visible()
    page.unroute("**/upload")
    expect(upload_error).to_contain_text("does not match file content")
    expect(button).to_be_enabled()

    # No credits: the reason and a way to buy more.
    upload("essay.docx", _docx_bytes())
    expect(upload_error).to_contain_text("not enough credits")
    expect(balance).to_contain_text("0 credits")

    # A good upload clears the old error, resets the file field, and the header shows
    # the reserved credit at once (singular).
    ledger.entries.append({"tenant_id": TENANT_ID, "delta": 2, "reason": "purchase"})
    upload("essay.docx", _docx_bytes())
    expect(upload_error).to_have_text("")
    expect(page.locator("#job-summary")).to_have_text("1 in progress")
    expect(balance).to_contain_text("1 credit —")
    assert page.locator("input[type=file]").evaluate("el => el.files.length") == 0

    # A rejected request (403) lands in the alert region and survives a poll.
    good_headers = page.locator("form.upload").get_attribute("hx-headers")
    page.locator("form.upload").evaluate(
        """form => form.setAttribute('hx-headers', '{"X-CSRF-Token": "wrong"}')"""
    )
    upload("a.docx", b"x")
    expect(htmx_error).to_have_text("CSRF token missing or invalid")
    page.locator("form.upload").evaluate(
        "(form, value) => form.setAttribute('hx-headers', value)", good_headers
    )

    # A poll that crashes (unhandled 500): a plain sentence in the poll's own region,
    # gone at the next good poll. The upload's 403 is left alone throughout.
    jobs.crash = True
    expect(poll_error).to_have_text(
        "Something went wrong on our side. Try again in a moment.", timeout=WAIT_MS
    )
    jobs.crash = False
    expect(poll_error).to_have_text("", timeout=WAIT_MS)
    expect(htmx_error).to_have_text("CSRF token missing or invalid")

    # A poll that gets no answer at all (server down): editta.js says so, then recovers.
    page.route("**/jobs-fragment", lambda route: route.abort())
    expect(poll_error).to_contain_text("Can't reach Editta", timeout=WAIT_MS)
    page.unroute("**/jobs-fragment")
    expect(poll_error).to_have_text("", timeout=WAIT_MS)

    # An upload that gets no answer.
    page.route("**/upload", lambda route: route.abort())
    upload("essay.docx", _docx_bytes())
    expect(htmx_error).to_contain_text("Couldn't reach Editta")
    page.unroute("**/upload")

    # The session ends while the page polls: htmx follows HX-Redirect to sign-in.
    context.clear_cookies()
    expect(page).to_have_url(f"{tenant}/login", timeout=WAIT_MS)

    assert script_errors == []
    context.close()
