"""Magic-link authentication and user invites (JSON API — see api/routes/web.py for the
browser-facing HTML flow). Invite-only: see services/accounts.py for the access model.
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status

from editta.api.deps import (
    SESSION_COOKIE_NAME,
    get_account_service,
    get_current_auth,
    get_email_sender,
    get_settings,
)
from editta.api.middleware import SIGN_IN_LINK_LIMIT, limiter, new_csrf_cookie_value, verify_csrf
from editta.api.schemas import (
    Envelope,
    InviteRequest,
    InviteResponse,
    MagicLinkRequest,
    MagicLinkRequestResponse,
    MagicLinkVerifyRequest,
    SessionResponse,
)
from editta.config import Settings
from editta.domain.errors import PermissionDenied
from editta.domain.ports import EmailSender
from editta.services.accounts import (
    SESSION_MAX_AGE_SECONDS,
    AccountService,
    AuthenticatedUser,
    InvalidMagicLink,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/magic-link", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(SIGN_IN_LINK_LIMIT)
async def request_magic_link(
    request: Request,  # required by the rate limiter
    body: MagicLinkRequest,
    background: BackgroundTasks,
    account_service: Annotated[AccountService, Depends(get_account_service)],
    email_sender: Annotated[EmailSender, Depends(get_email_sender)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Envelope[MagicLinkRequestResponse]:
    # The link goes to the inbox only — never into this response (that let anyone who
    # knew a tenant id sign in). Same answer, and the same latency, whether or not the
    # account exists: the email is sent after the response, in the background.
    login = await account_service.request_login(
        tenant_id=body.tenant_id, email=body.email, base_url=settings.app_base_url
    )
    if login is not None:
        background.add_task(
            email_sender.send_magic_link, to_email=login.to_email, link_url=login.url
        )
    return Envelope.ok(MagicLinkRequestResponse())


@router.post("/verify")
async def verify_magic_link(
    body: MagicLinkVerifyRequest,
    response: Response,
    account_service: Annotated[AccountService, Depends(get_account_service)],
) -> Envelope[SessionResponse]:
    try:
        user = await account_service.complete_login(body.token)
    except InvalidMagicLink as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    session_token = account_service.issue_session_cookie(
        tenant_id=user.tenant_id, user_id=user.user_id, email=user.email
    )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
    )
    # Readable by JS on purpose — double-submit CSRF pattern (api/middleware.py).
    response.set_cookie(
        "editta_csrf",
        new_csrf_cookie_value(),
        httponly=False,
        secure=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
    )
    return Envelope.ok(
        SessionResponse(tenant_id=user.tenant_id, user_id=user.user_id, email=user.email)
    )


@router.post("/invite", status_code=status.HTTP_201_CREATED, dependencies=[Depends(verify_csrf)])
async def invite_user(
    body: InviteRequest,
    auth: Annotated[AuthenticatedUser, Depends(get_current_auth)],
    account_service: Annotated[AccountService, Depends(get_account_service)],
) -> Envelope[InviteResponse]:
    """Admins add a user to their own tenant; the user then signs in by email."""
    try:
        user = await account_service.invite_user(actor=auth, email=body.email, role=body.role)
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return Envelope.ok(InviteResponse(user_id=user.user_id, email=user.email, role=user.role))
