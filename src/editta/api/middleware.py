"""CSRF, rate limiting, security headers, request-size cap. Log-scrubbing lives in the
structlog processor chain configured in api/main.py, not here.
"""

import hmac
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Header, HTTPException, Request, Response, status
from slowapi import Limiter
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from editta.adapters.parsing.safety import MAX_UPLOAD_BYTES
from editta.api.deps import SESSION_COOKIE_NAME, get_settings
from editta.config import rate_limit_storage_uri
from editta.services.accounts import session_rate_limit_subject

# The upload limit plus a MiB of multipart framing and form fields; the precise 25 MB
# check on the file itself stays in validate_upload.
MAX_REQUEST_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024


def new_csrf_cookie_value() -> str:
    return secrets.token_urlsafe(32)


_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    # unpkg.com: htmx is loaded from CDN, not vendored (PLAN.md: "no SPA build chain
    # until warranted"). 'unsafe-inline' on style only — base.html's <style> block,
    # never relaxed for script.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' https://unpkg.com; style-src 'self' 'unsafe-inline'"
    ),
}


def _rate_limit_key(request: Request) -> str:
    # Signed-in users are limited per user; everything else (including sign-in link
    # requests) per IP. Only a *validly signed* cookie counts: keying on the raw cookie
    # let a client send a fresh made-up cookie per request and never hit a limit.
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if session_cookie:
        user_id = session_rate_limit_subject(session_cookie, get_settings().session_secret)
        if user_id is not None:
            return f"user:{user_id}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


def build_limiter(storage_uri: str | None) -> Limiter:
    """With no `storage_uri` the counters live in this process. With one, every API
    process counts in the same store, so the limits mean what they say. If that store is
    unreachable the check is skipped (requests are not failed): limits lapse during the
    outage rather than taking the site down with it."""
    return Limiter(
        key_func=_rate_limit_key,
        default_limits=["120/minute"],
        storage_uri=storage_uri,
        swallow_errors=storage_uri is not None,
    )


limiter = build_limiter(rate_limit_storage_uri())

# Each sign-in link request can send an email to an inbox that is not the caller's, so it
# gets a much tighter limit than the default. Keyed per IP like the rest: behind a proxy
# uvicorn does not trust, that is one bucket for everyone -- see --forwarded-allow-ips.
SIGN_IN_LINK_LIMIT = "10/minute"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers[header] = value
        return response


class BodySizeLimitMiddleware:
    """Refuses request bodies over `max_bytes` with 413 while they stream in.

    FastAPI parses a multipart body before it runs dependencies, so without this an
    anonymous POST of any size is spooled to disk before the 401, and `file.read()` loads
    a signed-in user's upload into RAM before validate_upload sees it. Pure ASGI because
    only the raw `receive` channel can stop a chunked body midway. It answers 413 itself:
    an exception from `receive` would be turned into a 400 by FastAPI's body parsing.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            await self._too_large(scope, receive, send)
            return

        received = 0
        exceeded = False
        started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def limited_send(message: Message) -> None:
            nonlocal started
            if exceeded:  # whatever the app answers to a cut-off body is replaced below
                return
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not started:
            await self._too_large(scope, receive, send)

    async def _too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        detail = f"request body exceeds {self.max_bytes // (1024 * 1024)} MB"
        await JSONResponse({"detail": detail}, status_code=413)(scope, receive, send)


async def verify_csrf(request: Request, x_csrf_token: str | None = Header(default=None)) -> None:
    """Double-submit cookie check. Only applies to mutating methods on session-cookie
    (browser) requests — a missing session cookie means there's no session to forge."""
    if request.method not in _MUTATING_METHODS:
        return
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if session_cookie is None:
        return
    csrf_cookie = request.cookies.get("editta_csrf")
    if not csrf_cookie or not x_csrf_token or not hmac.compare_digest(csrf_cookie, x_csrf_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid")
