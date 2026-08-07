"""FastAPI route contract tests. dependency_overrides swap DB/blob adapters for the
same in-memory fakes used in tests/services, so these run without Postgres/S3/Docker
— exactly the "contract tests per endpoint" PLAN.md §8 calls for.
"""

import io
from uuid import uuid4

import docx
import pytest
from fastapi.testclient import TestClient

from editta.api.deps import (
    get_account_service,
    get_audit_repo,
    get_blob_store,
    get_job_repo,
    get_manuscript_repo,
    get_report_repo,
)
from editta.api.main import app
from editta.services.accounts import AccountService
from tests.services.fakes import (
    FakeAnalysisJobRepo,
    FakeAuditRepo,
    FakeBlobStore,
    FakeManuscriptRepo,
    FakeReportRepo,
    FakeTenantRepo,
    FakeUserRepo,
)

TEST_SESSION_SECRET = "contract-test-secret"  # noqa: S105 -- test fixture, not a real secret


def _docx_bytes() -> bytes:
    document = docx.Document()
    document.add_paragraph("a manuscript for contract testing")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def client() -> TestClient:
    manuscripts = FakeManuscriptRepo()
    jobs = FakeAnalysisJobRepo()
    reports = FakeReportRepo()
    audit = FakeAuditRepo()
    blobs = FakeBlobStore()
    account_service = AccountService(
        tenants=FakeTenantRepo(),  # type: ignore[arg-type]
        users=FakeUserRepo(),  # type: ignore[arg-type]
        session_secret=TEST_SESSION_SECRET,
    )

    app.dependency_overrides[get_manuscript_repo] = lambda: manuscripts
    app.dependency_overrides[get_job_repo] = lambda: jobs
    app.dependency_overrides[get_report_repo] = lambda: reports
    app.dependency_overrides[get_audit_repo] = lambda: audit
    app.dependency_overrides[get_blob_store] = lambda: blobs
    app.dependency_overrides[get_account_service] = lambda: account_service

    # base_url is https so httpx's cookie jar actually stores/resends our Secure=True
    # session and CSRF cookies — over plain http they'd be silently dropped, same as
    # a real browser would drop them.
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client

    app.dependency_overrides.clear()


def _login(client: TestClient) -> None:
    tenant_id = uuid4()
    link = client.post(
        "/auth/magic-link", json={"tenant_id": str(tenant_id), "email": "editor@example.com"}
    )
    token = link.json()["data"]["magic_link_token"]
    verify = client.post("/auth/verify", json={"token": token})
    assert verify.status_code == 200


def test_upload_without_session_cookie_is_rejected(client: TestClient) -> None:
    response = client.post("/manuscripts", files={"file": ("essay.docx", b"data")})
    assert response.status_code == 401


def test_job_status_without_session_cookie_is_rejected(client: TestClient) -> None:
    response = client.get(f"/manuscripts/jobs/{uuid4()}")
    assert response.status_code == 401


def test_magic_link_login_then_upload_then_status_round_trips(client: TestClient) -> None:
    _login(client)
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


def test_upload_without_csrf_token_is_rejected(client: TestClient) -> None:
    _login(client)

    response = client.post("/manuscripts", files={"file": ("essay.docx", b"data")})

    assert response.status_code == 403


def test_report_download_before_report_exists_is_not_found(client: TestClient) -> None:
    _login(client)
    csrf_token = client.cookies.get("editta_csrf")
    upload = client.post(
        "/manuscripts",
        files={"file": ("essay.docx", _docx_bytes())},
        headers={"X-CSRF-Token": csrf_token},
    )
    job_id = upload.json()["data"]["job_id"]

    response = client.get(f"/manuscripts/jobs/{job_id}/report")

    assert response.status_code == 404
