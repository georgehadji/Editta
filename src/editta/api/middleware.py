"""CSRF, rate limiting, security headers. Log-scrubbing lives in the structlog
processor chain configured in api/main.py, not here.
"""

import hmac
from collections.abc import Awaitable, Callable

from fastapi import Header, HTTPException, Request, Response, status
from slowapi import Limiter
from starlette.middleware.base import BaseHTTPMiddleware

from editta.api.deps import SESSION_COOKIE_NAME

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "Content-Security-Policy": "default-src 'self'",
}


def _rate_limit_key(request: Request) -> str:
    # Session cookie doubles as a stable per-tenant key once logged in; unauthenticated
    # requests (magic-link issuance) fall back to per-IP, same as slowapi's default.
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if session_cookie:
        return f"session:{session_cookie}"
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


async def verify_csrf(
    request: Request, x_csrf_token: str | None = Header(default=None)
) -> None:
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
