"""Server-rendered Jinja2 + HTMX web UI. Tenant-scoped URLs (/t/{tenant_id}/...)
because there's no cross-tenant "log in by email alone" resolution built yet — a
tenant admin shares this tenant-specific login URL with their editors directly.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from editta.adapters.email.console import ConsoleEmailSender
from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    AuditEventRepository,
    CreditLedgerRepository,
    ManuscriptRepository,
    ReportRepository,
    TenantRepository,
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
    get_tenant_repo,
)
from editta.api.middleware import new_csrf_cookie_value, verify_csrf
from editta.config import Settings
from editta.domain.errors import InsufficientCredits, ParseError
from editta.domain.ports import BlobStore, EmailSender
from editta.services.accounts import (
    SESSION_MAX_AGE_SECONDS,
    AccountService,
    InvalidMagicLink,
)
from editta.services.uploads import submit_manuscript

router = APIRouter(dependencies=[Depends(verify_csrf)])


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
    # Invite-only: a link is emailed only if this email already has an account here,
    # and the page reads the same either way (no account enumeration).
    link_url = await account_service.request_login(
        tenant_id=tenant_id, email=email, sender=email_sender, base_url=settings.app_base_url
    )
    # Local development only: show the link instead of making you read the console.
    # Never in any other environment — a displayed link bypasses proof of the inbox.
    show_dev_link = settings.environment == "development" and isinstance(
        email_sender, ConsoleEmailSender
    )
    dev_link = link_url if show_dev_link else None
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
        user = await account_service.complete_login(token, expected_tenant_id=tenant_id)
    except InvalidMagicLink as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    session_token = account_service.issue_session_cookie(
        tenant_id=tenant_id, user_id=user.user_id, email=user.email
    )

    response = RedirectResponse(f"/t/{tenant_id}/dashboard", status_code=status.HTTP_303_SEE_OTHER)
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
    tenants: Annotated[TenantRepository, Depends(get_tenant_repo)],
    ledger: Annotated[CreditLedgerRepository, Depends(get_ledger_repo)],
    blobs: Annotated[BlobStore, Depends(get_blob_store)],
    settings: Annotated[Settings, Depends(get_settings)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> HTMLResponse:
    _require_matching_tenant(auth, tenant_id)

    try:
        await submit_manuscript(
            tenant_id=tenant_id,
            actor=auth.email,
            data=await file.read(),
            filename=file.filename,
            manuscripts=manuscripts,
            jobs=jobs,
            audit=audit,
            tenants=tenants,
            ledger=ledger,
            blobs=blobs,
            retention_days=settings.manuscript_retention_days,
            job_cost_credits=settings.job_cost_credits,
        )
    except ParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except InsufficientCredits as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc

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
