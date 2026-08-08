"""Server-rendered Jinja2 + HTMX web UI. Tenant-scoped URLs (/t/{tenant_id}/...)
because there's no cross-tenant "log in by email alone" resolution built yet — a
tenant admin shares this tenant-specific login URL with their editors directly.
"""

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from editta.adapters.email.console import ConsoleEmailSender
from editta.adapters.parsing.safety import validate_upload
from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    AuditEventRepository,
    CreditLedgerRepository,
    ManuscriptRepository,
    ReportRepository,
)
from editta.api.deps import (
    SESSION_COOKIE_NAME,
    AuthContext,
    get_account_service,
    get_audit_repo,
    get_blob_store,
    get_current_auth,
    get_email_sender,
    get_job_repo,
    get_ledger_repo,
    get_manuscript_repo,
    get_report_repo,
    get_settings,
    get_templates,
)
from editta.api.middleware import new_csrf_cookie_value, verify_csrf
from editta.config import Settings
from editta.domain.errors import ParseError
from editta.domain.ports import BlobStore, EmailSender
from editta.services.accounts import (
    SESSION_MAX_AGE_SECONDS,
    AccountService,
    InvalidMagicLink,
)

router = APIRouter(dependencies=[Depends(verify_csrf)])


async def _single_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _require_matching_tenant(auth: AuthContext, tenant_id: UUID) -> None:
    if auth.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "wrong tenant")


@router.get("/t/{tenant_id}/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    tenant_id: UUID,
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"tenant_id": tenant_id})


@router.post("/t/{tenant_id}/login", response_class=HTMLResponse)
async def request_login_link(
    request: Request,
    tenant_id: UUID,
    email: Annotated[str, Form()],
    account_service: Annotated[AccountService, Depends(get_account_service)],
    email_sender: Annotated[EmailSender, Depends(get_email_sender)],
    settings: Annotated[Settings, Depends(get_settings)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> HTMLResponse:
    await account_service.invite_user(tenant_id, email)
    token = account_service.issue_magic_link_token(tenant_id=tenant_id, email=email)
    link_url = f"{settings.app_base_url}/t/{tenant_id}/auth/click?token={token}"
    await email_sender.send_magic_link(to_email=email, link_url=link_url)

    dev_link = link_url if isinstance(email_sender, ConsoleEmailSender) else None
    return templates.TemplateResponse(
        request, "check_email.html", {"email": email, "dev_magic_link": dev_link}
    )


@router.get("/t/{tenant_id}/auth/click")
async def verify_login_link(
    tenant_id: UUID,
    token: str,
    account_service: Annotated[AccountService, Depends(get_account_service)],
) -> RedirectResponse:
    try:
        verified_tenant_id, email = account_service.verify_magic_link_token(token)
    except InvalidMagicLink as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    if verified_tenant_id != tenant_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token does not match tenant")

    user = await account_service.invite_user(tenant_id, email)
    session_token = account_service.issue_session_cookie(
        tenant_id=tenant_id, user_id=user.id, email=email
    )

    response = RedirectResponse(
        f"/t/{tenant_id}/dashboard", status_code=status.HTTP_303_SEE_OTHER
    )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
    )
    response.set_cookie(
        "editta_csrf",
        new_csrf_cookie_value(),
        httponly=False,
        secure=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
    )
    return response


@router.get("/t/{tenant_id}/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    tenant_id: UUID,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    jobs: Annotated[AnalysisJobRepository, Depends(get_job_repo)],
    ledger: Annotated[CreditLedgerRepository, Depends(get_ledger_repo)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> HTMLResponse:
    _require_matching_tenant(auth, tenant_id)
    job_list = await jobs.list_for_tenant(tenant_id)
    balance = await ledger.balance(tenant_id)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "tenant_id": tenant_id,
            "jobs": job_list,
            "credit_balance": balance,
            "csrf_token": request.cookies.get("editta_csrf", ""),
        },
    )


@router.get("/t/{tenant_id}/jobs-fragment", response_class=HTMLResponse)
async def jobs_fragment(
    request: Request,
    tenant_id: UUID,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    jobs: Annotated[AnalysisJobRepository, Depends(get_job_repo)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> HTMLResponse:
    _require_matching_tenant(auth, tenant_id)
    job_list = await jobs.list_for_tenant(tenant_id)
    return templates.TemplateResponse(
        request, "_job_list.html", {"tenant_id": tenant_id, "jobs": job_list}
    )


@router.post("/t/{tenant_id}/upload", response_class=HTMLResponse)
async def upload_via_web(
    request: Request,
    tenant_id: UUID,
    file: UploadFile,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    manuscripts: Annotated[ManuscriptRepository, Depends(get_manuscript_repo)],
    jobs: Annotated[AnalysisJobRepository, Depends(get_job_repo)],
    audit: Annotated[AuditEventRepository, Depends(get_audit_repo)],
    blobs: Annotated[BlobStore, Depends(get_blob_store)],
    settings: Annotated[Settings, Depends(get_settings)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> HTMLResponse:
    _require_matching_tenant(auth, tenant_id)

    data = await file.read()
    filename = file.filename or "manuscript.docx"
    try:
        validate_upload(data, filename)
    except ParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    sha256 = hashlib.sha256(data).hexdigest()
    retention_until = datetime.now(UTC) + timedelta(days=settings.manuscript_retention_days)
    manuscript = await manuscripts.create(
        tenant_id=tenant_id,
        title=filename,
        blob_key=f"{tenant_id}/manuscripts/{sha256}/{filename}",
        sha256=sha256,
        retention_until=retention_until,
    )
    await blobs.put(manuscript.blob_key, _single_chunk(data))
    await jobs.create(tenant_id=tenant_id, manuscript_id=manuscript.id)
    await audit.append(
        tenant_id=tenant_id, actor=auth.email, action="upload", subject=str(manuscript.id)
    )

    job_list = await jobs.list_for_tenant(tenant_id)
    return templates.TemplateResponse(
        request, "_job_list.html", {"tenant_id": tenant_id, "jobs": job_list}
    )


@router.get("/t/{tenant_id}/jobs/{job_id}/download")
async def download_report(
    tenant_id: UUID,
    job_id: UUID,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    jobs: Annotated[AnalysisJobRepository, Depends(get_job_repo)],
    reports: Annotated[ReportRepository, Depends(get_report_repo)],
    audit: Annotated[AuditEventRepository, Depends(get_audit_repo)],
    blobs: Annotated[BlobStore, Depends(get_blob_store)],
) -> RedirectResponse:
    _require_matching_tenant(auth, tenant_id)
    job = await jobs.get_by_id(tenant_id, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    report = await reports.get_by_job_id(job_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not ready")
    url = await blobs.presigned_get_url(report.blob_key, expires_in=3600)
    await audit.append(
        tenant_id=tenant_id, actor=auth.email, action="download", subject=str(report.id)
    )
    return RedirectResponse(url)
