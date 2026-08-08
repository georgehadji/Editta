"""Magic-link authentication endpoints (JSON API — see api/routes/web.py for the
browser-facing HTML flow, which actually emails the link via EmailSender).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status

from editta.api.deps import SESSION_COOKIE_NAME, get_account_service
from editta.api.middleware import new_csrf_cookie_value
from editta.api.schemas import (
    Envelope,
    MagicLinkRequest,
    MagicLinkRequestResponse,
    MagicLinkVerifyRequest,
    SessionResponse,
)
from editta.services.accounts import (
    SESSION_MAX_AGE_SECONDS,
    AccountService,
    InvalidMagicLink,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/magic-link")
async def request_magic_link(
    body: MagicLinkRequest,
    account_service: Annotated[AccountService, Depends(get_account_service)],
) -> Envelope[MagicLinkRequestResponse]:
    await account_service.invite_user(body.tenant_id, body.email)
    token = account_service.issue_magic_link_token(tenant_id=body.tenant_id, email=body.email)
    # JSON API consumers own their own delivery/UI; returning the token directly here
    # is intentional, not a stand-in — contrast with api/routes/web.py's HTML flow,
    # which emails it via EmailSender since a browser user can't "read" a JSON body.
    return Envelope.ok(MagicLinkRequestResponse(magic_link_token=token))


@router.post("/verify")
async def verify_magic_link(
    body: MagicLinkVerifyRequest,
    response: Response,
    account_service: Annotated[AccountService, Depends(get_account_service)],
) -> Envelope[SessionResponse]:
    try:
        tenant_id, email = account_service.verify_magic_link_token(body.token)
    except InvalidMagicLink as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    user = await account_service.invite_user(tenant_id, email)
    session_token = account_service.issue_session_cookie(
        tenant_id=tenant_id, user_id=user.id, email=email
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
    return Envelope.ok(SessionResponse(tenant_id=tenant_id, user_id=user.id, email=email))
