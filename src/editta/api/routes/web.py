"""Server-rendered Jinja2 + HTMX web UI. Tenant-scoped URLs (/t/{tenant_id}/...)
because there's no cross-tenant "log in by email alone" resolution built yet — a
tenant admin shares this tenant-specific login URL with their editors directly.
"""

import hmac
import secrets
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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
from editta.api.middleware import SIGN_IN_LINK_LIMIT, limiter, new_csrf_cookie_value, verify_csrf
from editta.config import Settings
from editta.domain.artifacts import ArtifactKind
from editta.domain.errors import InsufficientCredits, NotFound, ParseError
from editta.domain.ports import BlobStore, EmailSender
from editta.services.accounts import (
    SESSION_MAX_AGE_SECONDS,
    AccountService,
    InvalidMagicLink,
)
from editta.services.reports import report_download
from editta.services.uploads import submit_manuscript

# Signed-in pages: session cookie + double-submit CSRF header on every mutation.
router = APIRouter(dependencies=[Depends(verify_csrf)])
# Sign-in pages run before a session exists, so the session CSRF check does not fit
# them: a leftover cookie from an old session used to 403 the sign-in form. They are
# protected on their own terms — requesting a link only ever emails an existing
# account, and confirming one requires the login nonce (see confirm_login).
public_router = APIRouter()

LOGIN_NONCE_COOKIE = "editta_login_nonce"
LOGIN_NONCE_MAX_AGE_SECONDS = 15 * 60


def _upload_error(
    request: Request, templates: Jinja2Templates, *, message: str, status_code: int
) -> HTMLResponse:
    """htmx does not swap 4xx responses, so a refused upload used to change nothing on
    the page. For htmx requests, answer 200 with an error fragment retargeted above the
    form (HX-Retarget / HX-Reswap); plain clients keep the real 402/422 status."""
    response = templates.TemplateResponse(
        request, "_upload_error.html", {"message": message, "payment_required": status_code == 402}
    )
    if request.headers.get("HX-Request") == "true":
        response.headers["HX-Retarget"] = "#upload-error"
        response.headers["HX-Reswap"] = "innerHTML"
    else:
        response.status_code = status_code
    return response


def _require_matching_tenant(auth: AuthContext, tenant_id: UUID) -> None:
    if auth.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "wrong tenant")


@public_router.get("/t/{tenant_id}/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    tenant_id: UUID,
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"tenant_id": tenant_id})


@public_router.post("/t/{tenant_id}/login", response_class=HTMLResponse)
@limiter.limit(SIGN_IN_LINK_LIMIT)
async def request_login_link(
    request: Request,
    background: BackgroundTasks,
    tenant_id: UUID,
    email: Annotated[str, Form()],
    account_service: Annotated[AccountService, Depends(get_account_service)],
    email_sender: Annotated[EmailSender, Depends(get_email_sender)],
    settings: Annotated[Settings, Depends(get_settings)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> HTMLResponse:
    # Invite-only: a link is emailed only if this email already has an account here,
    # and the page reads the same either way (no account enumeration).
    login = await account_service.request_login(
        tenant_id=tenant_id, email=email, base_url=settings.app_base_url
    )
    if login is not None:  # sent after responding: no timing difference to observe
        background.add_task(
            email_sender.send_magic_link, to_email=login.to_email, link_url=login.url
        )
    link_url = login.url if login is not None else None
    # Local development only: show the link instead of making you read the console.
    # Never in any other environment — a displayed link bypasses proof of the inbox.
    show_dev_link = settings.environment == "development" and isinstance(
        email_sender, ConsoleEmailSender
    )
    dev_link = link_url if show_dev_link else None
    return templates.TemplateResponse(
        request, "check_email.html", {"email": email, "dev_magic_link": dev_link}
    )


@public_router.get("/t/{tenant_id}/auth/click", response_class=HTMLResponse)
async def confirm_login_page(
    request: Request,
    tenant_id: UUID,
    token: str,
    account_service: Annotated[AccountService, Depends(get_account_service)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> HTMLResponse:
    """Opening the emailed link does not sign anyone in; it shows who would be signed
    in, to which tenant, and asks to confirm.

    Signing in on a bare GET let an attacker send a victim *the attacker's own* fresh
    link: one click silently replaced the victim's session with the attacker's, and
    the victim's next upload — a confidential manuscript — landed in the attacker's
    tenant. It also let mail scanners that pre-open links consume them. The nonce
    cookie set here must accompany the confirming POST, which a third-party page
    cannot read or set, so a cross-site form cannot complete a login either.
    """
    try:
        # Check only: the confirming POST below spends the link.
        user = await account_service.complete_login(
            token, expected_tenant_id=tenant_id, consume=False
        )
    except InvalidMagicLink as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    nonce = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request,
        "confirm_login.html",
        {"tenant_id": tenant_id, "email": user.email, "token": token, "nonce": nonce},
    )
    response.set_cookie(
        LOGIN_NONCE_COOKIE,
        nonce,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=LOGIN_NONCE_MAX_AGE_SECONDS,
        path=f"/t/{tenant_id}/auth/click",
    )
    return response


@public_router.post("/t/{tenant_id}/auth/click")
async def confirm_login(
    request: Request,
    tenant_id: UUID,
    token: Annotated[str, Form()],
    nonce: Annotated[str, Form()],
    account_service: Annotated[AccountService, Depends(get_account_service)],
) -> RedirectResponse:
    expected = request.cookies.get(LOGIN_NONCE_COOKIE)
    if not expected or not hmac.compare_digest(expected, nonce):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "open the sign-in link in this browser, then confirm"
        )
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
    response.delete_cookie(LOGIN_NONCE_COOKIE, path=f"/t/{tenant_id}/auth/click")
    return response


@router.post("/t/{tenant_id}/logout")
async def logout(
    request: Request,
    tenant_id: UUID,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
) -> Response:
    """Clears this browser's session. (Sessions are stateless signed cookies; revoking
    every session of a user means removing the user — see services/accounts.py.)"""
    login_url = f"/t/{tenant_id}/login"
    response: Response
    if request.headers.get("HX-Request") == "true":
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.headers["HX-Redirect"] = login_url
    else:
        response = RedirectResponse(login_url, status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.delete_cookie("editta_csrf")
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
            "email": auth.email,
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
    except (ParseError, InsufficientCredits) as exc:
        code = (
            status.HTTP_402_PAYMENT_REQUIRED
            if isinstance(exc, InsufficientCredits)
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        return _upload_error(request, templates, message=str(exc), status_code=code)

    job_list = await jobs.list_for_tenant(tenant_id)
    return templates.TemplateResponse(
        request, "_job_list.html", {"tenant_id": tenant_id, "jobs": job_list}
    )


@router.get("/t/{tenant_id}/jobs/{job_id}/artifacts/{kind}")
async def download_artifact(
    tenant_id: UUID,
    job_id: UUID,
    kind: ArtifactKind,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    jobs: Annotated[AnalysisJobRepository, Depends(get_job_repo)],
    manuscripts: Annotated[ManuscriptRepository, Depends(get_manuscript_repo)],
    reports: Annotated[ReportRepository, Depends(get_report_repo)],
    audit: Annotated[AuditEventRepository, Depends(get_audit_repo)],
    blobs: Annotated[BlobStore, Depends(get_blob_store)],
) -> RedirectResponse:
    _require_matching_tenant(auth, tenant_id)
    try:
        download = await report_download(
            tenant_id=tenant_id,
            job_id=job_id,
            kind=kind,
            actor=auth.email,
            jobs=jobs,
            manuscripts=manuscripts,
            reports=reports,
            audit=audit,
            blobs=blobs,
        )
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return RedirectResponse(download.url)


@router.get("/t/{tenant_id}/jobs/{job_id}/download")
async def download_report(
    tenant_id: UUID,
    job_id: UUID,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    jobs: Annotated[AnalysisJobRepository, Depends(get_job_repo)],
    manuscripts: Annotated[ManuscriptRepository, Depends(get_manuscript_repo)],
    reports: Annotated[ReportRepository, Depends(get_report_repo)],
    audit: Annotated[AuditEventRepository, Depends(get_audit_repo)],
    blobs: Annotated[BlobStore, Depends(get_blob_store)],
) -> RedirectResponse:
    """Kept for the links already on the job list."""
    return await download_artifact(
        tenant_id, job_id, ArtifactKind.REPORT, auth, jobs, manuscripts, reports, audit, blobs
    )
