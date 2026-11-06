"""FastAPI route contract tests. dependency_overrides swap DB/blob adapters for the
same in-memory fakes used in tests/services, so these run without Postgres/S3/Docker
— exactly the "contract tests per endpoint" PLAN.md §8 calls for.
"""

import asyncio
import io
from uuid import uuid4

import docx
import pytest
from fastapi.testclient import TestClient

from editta.api.deps import (
    get_account_service,
    get_audit_repo,
    get_blob_store,
    get_email_sender,
    get_job_repo,
    get_ledger_repo,
    get_manuscript_repo,
    get_report_repo,
    get_tenant_repo,
)
from editta.api.main import app
from tests.services.fakes import (
    FakeAnalysisJobRepo,
    FakeAuditRepo,
    FakeBlobStore,
    FakeLedgerRepo,
    FakeManuscriptRepo,
    FakeReportRepo,
    FakeTenantRepo,
    FakeUserRepo,
    RecordingEmailSender,
    make_account_service,
)

TEST_SESSION_SECRET = "contract-test-secret"  # noqa: S105 -- test fixture, not a real secret


def _docx_bytes() -> bytes:
    document = docx.Document()
    document.add_paragraph("a manuscript for contract testing")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


TENANT_ID = uuid4()
EDITOR = "editor@example.com"
ADMIN = "admin@example.com"


class Harness:
    def __init__(self) -> None:
        self.users = FakeUserRepo()
        self.inbox = RecordingEmailSender()
        self.ledger = FakeLedgerRepo()
        self.account_service = make_account_service(self.users, secret=TEST_SESSION_SECRET)

    def credit(self, amount: int) -> None:
        self.ledger.entries.append({"tenant_id": TENANT_ID, "delta": amount, "reason": "purchase"})


@pytest.fixture
def harness() -> Harness:
    return Harness()


@pytest.fixture
def client(harness: Harness) -> TestClient:
    manuscripts = FakeManuscriptRepo()
    jobs = FakeAnalysisJobRepo()
    reports = FakeReportRepo()
    audit = FakeAuditRepo()
    blobs = FakeBlobStore()
    tenants = FakeTenantRepo()
    asyncio.run(harness.users.create(tenant_id=TENANT_ID, email=EDITOR, role="editor"))
    asyncio.run(harness.users.create(tenant_id=TENANT_ID, email=ADMIN, role="admin"))
    harness.credit(5)

    app.dependency_overrides[get_manuscript_repo] = lambda: manuscripts
    app.dependency_overrides[get_job_repo] = lambda: jobs
    app.dependency_overrides[get_report_repo] = lambda: reports
    app.dependency_overrides[get_audit_repo] = lambda: audit
    app.dependency_overrides[get_blob_store] = lambda: blobs
    app.dependency_overrides[get_tenant_repo] = lambda: tenants
    app.dependency_overrides[get_ledger_repo] = lambda: harness.ledger
    app.dependency_overrides[get_account_service] = lambda: harness.account_service
    app.dependency_overrides[get_email_sender] = lambda: harness.inbox

    # base_url is https so httpx's cookie jar actually stores/resends our Secure=True
    # session and CSRF cookies — over plain http they'd be silently dropped, same as
    # a real browser would drop them.
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client

    app.dependency_overrides.clear()


def _login(client: TestClient, harness: Harness, email: str = EDITOR) -> None:
    link = client.post("/auth/magic-link", json={"tenant_id": str(TENANT_ID), "email": email})
    assert link.status_code == 202
    verify = client.post("/auth/verify", json={"token": harness.inbox.last_token_for(email)})
    assert verify.status_code == 200


def test_upload_without_session_cookie_is_rejected(client: TestClient) -> None:
    response = client.post("/manuscripts", files={"file": ("essay.docx", b"data")})
    assert response.status_code == 401


def test_job_status_without_session_cookie_is_rejected(client: TestClient) -> None:
    response = client.get(f"/manuscripts/jobs/{uuid4()}")
    assert response.status_code == 401


def test_magic_link_login_then_upload_then_status_round_trips(
    client: TestClient, harness: Harness
) -> None:
    _login(client, harness)
    csrf_token = client.cookies.get("editta_csrf")

    upload = client.post(
        "/manuscripts",
        files={"file": ("essay.docx", _docx_bytes())},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert upload.status_code == 201
    body = upload.json()
    assert body["success"] is True
    job_id = body["data"]["job_id"]

    status_response = client.get(f"/manuscripts/jobs/{job_id}")
    assert status_response.status_code == 200
    assert status_response.json()["data"]["job_id"] == job_id


def test_upload_without_csrf_token_is_rejected(client: TestClient, harness: Harness) -> None:
    _login(client, harness)

    response = client.post("/manuscripts", files={"file": ("essay.docx", b"data")})

    assert response.status_code == 403


def test_report_download_before_report_exists_is_not_found(
    client: TestClient, harness: Harness
) -> None:
    _login(client, harness)
    csrf_token = client.cookies.get("editta_csrf")
    upload = client.post(
        "/manuscripts",
        files={"file": ("essay.docx", _docx_bytes())},
        headers={"X-CSRF-Token": csrf_token},
    )
    job_id = upload.json()["data"]["job_id"]

    response = client.get(f"/manuscripts/jobs/{job_id}/report")

    assert response.status_code == 404


def test_artifact_download_validates_the_kind_before_any_lookup(
    client: TestClient, harness: Harness
) -> None:
    _login(client, harness)
    job_id = uuid4()

    unknown = client.get(f"/manuscripts/jobs/{job_id}/artifacts/passwd")
    traversal = client.get(f"/manuscripts/jobs/{job_id}/artifacts/..%2F..%2Fsecret")
    known = client.get(f"/manuscripts/jobs/{job_id}/artifacts/report")

    assert unknown.status_code == 422
    assert traversal.status_code in (404, 422)
    assert known.status_code == 404  # a valid kind reaches the lookup: no such job


# --- invite-only access & credits --------------------------------------------------


def test_magic_link_response_never_contains_a_token(client: TestClient, harness: Harness) -> None:
    """Regression: the JSON API returned the sign-in token, so anyone who knew a
    tenant id could sign in as any email."""
    response = client.post("/auth/magic-link", json={"tenant_id": str(TENANT_ID), "email": EDITOR})

    assert response.status_code == 202
    assert response.json()["data"] == {"accepted": True}
    assert harness.inbox.last_token_for(EDITOR) not in response.text


def test_unknown_email_gets_the_same_answer_and_no_account(
    client: TestClient, harness: Harness
) -> None:
    known = client.post("/auth/magic-link", json={"tenant_id": str(TENANT_ID), "email": EDITOR})
    unknown = client.post(
        "/auth/magic-link", json={"tenant_id": str(TENANT_ID), "email": "intruder@example.org"}
    )

    assert (unknown.status_code, unknown.json()) == (known.status_code, known.json())
    assert [to for to, _ in harness.inbox.sent] == [EDITOR]
    assert asyncio.run(harness.users.get_by_email(TENANT_ID, "intruder@example.org")) is None


def test_upload_without_credits_is_payment_required(client: TestClient, harness: Harness) -> None:
    harness.ledger.entries.clear()
    _login(client, harness)

    response = client.post(
        "/manuscripts",
        files={"file": ("essay.docx", _docx_bytes())},
        headers={"X-CSRF-Token": client.cookies.get("editta_csrf")},
    )

    assert response.status_code == 402


def test_editors_cannot_invite(client: TestClient, harness: Harness) -> None:
    _login(client, harness, EDITOR)

    response = client.post(
        "/auth/invite",
        json={"email": "new@example.com"},
        headers={"X-CSRF-Token": client.cookies.get("editta_csrf")},
    )

    assert response.status_code == 403


def test_admin_invite_lets_the_new_user_sign_in(client: TestClient, harness: Harness) -> None:
    _login(client, harness, ADMIN)

    invited = client.post(
        "/auth/invite",
        json={"email": "new@example.com", "role": "editor"},
        headers={"X-CSRF-Token": client.cookies.get("editta_csrf")},
    )
    client.cookies.clear()
    _login(client, harness, "new@example.com")

    assert invited.status_code == 201
    assert invited.json()["data"]["role"] == "editor"


def test_removed_user_loses_access_on_the_next_request(
    client: TestClient, harness: Harness
) -> None:
    _login(client, harness)
    harness.users.remove(TENANT_ID, EDITOR)

    response = client.get(f"/manuscripts/jobs/{uuid4()}")

    assert response.status_code == 401


def test_smtp_failure_does_not_reveal_that_the_account_exists(harness: Harness) -> None:
    """Delivery happens after the response: a failing mail server must not turn the
    answer for a real account into a 500 (unknown emails would still get 202)."""

    class BrokenSender:
        async def send_magic_link(self, *, to_email: str, link_url: str) -> None:
            raise ConnectionError("SMTP down")

    asyncio.run(harness.users.create(tenant_id=TENANT_ID, email=EDITOR))
    app.dependency_overrides[get_account_service] = lambda: harness.account_service
    app.dependency_overrides[get_email_sender] = BrokenSender
    try:
        with TestClient(
            app, base_url="https://testserver", raise_server_exceptions=False
        ) as test_client:
            known = test_client.post(
                "/auth/magic-link", json={"tenant_id": str(TENANT_ID), "email": EDITOR}
            )
            unknown = test_client.post(
                "/auth/magic-link",
                json={"tenant_id": str(TENANT_ID), "email": "nobody@example.org"},
            )
    finally:
        app.dependency_overrides.clear()

    assert (
        (known.status_code, known.json())
        == (unknown.status_code, unknown.json())
        == (
            202,
            {"success": True, "data": {"accepted": True}, "error": None, "meta": None},
        )
    )
