"""CSRF, rate limiting, security headers. Log-scrubbing lives in the structlog
processor chain configured in api/main.py, not here.
"""

import hmac
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Header, HTTPException, Request, Response, status
from slowapi import Limiter
from starlette.middleware.base import BaseHTTPMiddleware

from editta.api.deps import SESSION_COOKIE_NAME, get_settings
from editta.services.accounts import session_rate_limit_subject


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


limiter = Limiter(key_func=_rate_limit_key, default_limits=["120/minute"])


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers[header] = value
        return response


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
