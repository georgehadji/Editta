"""FastAPI app factory: composition root wiring routers, middleware, rate limiting."""

import html
import logging
import mimetypes
import re

import structlog
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from editta.api.deps import STATIC_DIR
from editta.api.middleware import (
    MAX_REQUEST_BYTES,
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    limiter,
)
from editta.api.routes import auth, billing, health, manuscripts, web
from editta.observability import configure_logging

logger = structlog.get_logger(__name__)

_SECRET_QUERY = re.compile(r"(?<=[?&]token=)[^&\s]+")
_TENANT_PAGE = re.compile(r"^/t/([0-9a-fA-F-]{36})/")


class _RedactTokens(logging.Filter):
    """Sign-in links carry a live token in the query string, and uvicorn's access log
    prints the whole path. Redact the secret, keep the line."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _SECRET_QUERY.sub("REDACTED", arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactTokens())

configure_logging("api")

app = FastAPI(title="Editta API")
app.state.limiter = limiter
# slowapi's handler is typed for RateLimitExceeded specifically; Starlette's stub
# wants a general Exception handler — safe in practice, Starlette dispatches by the
# exact registered type.
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]


@app.exception_handler(StarletteHTTPException)
async def _browser_errors(request: Request, exc: StarletteHTTPException) -> Response:
    """Errors on the signed-in pages (/t/{tenant}/...) answer the browser, not a JSON client.

    A 401 sends the user to that tenant's sign-in page: htmx follows `HX-Redirect`, a plain
    GET gets a 303. Any other error to an htmx request comes back as a line of text that
    base.html's response handling puts in the alert region. A failed poll's text goes to
    its own region instead, which the next good poll clears, so a passing hiccup does not
    stay on screen and an upload's error is not wiped by a poll. The JSON API keeps
    FastAPI's {"detail": ...} body."""
    tenant = _TENANT_PAGE.match(request.url.path)
    htmx = request.headers.get("HX-Request") == "true"
    if tenant and exc.status_code == 401:
        login = f"/t/{tenant.group(1)}/login"
        if htmx:
            return Response(status_code=401, headers={"HX-Redirect": login})
        if request.method == "GET":
            return RedirectResponse(login, status_code=303)
    if tenant and htmx:
        return _htmx_error(request, str(exc.detail), exc.status_code)
    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def _browser_crashes(request: Request, exc: Exception) -> Response:
    """An unhandled error. Starlette still logs and re-raises it; this only words the
    answer, which for htmx on a signed-in page was a bare "Internal Server Error" in the
    upload's alert region, even when a poll failed."""
    if _TENANT_PAGE.match(request.url.path) and request.headers.get("HX-Request") == "true":
        return _htmx_error(request, "Something went wrong on our side. Try again in a moment.", 500)
    return PlainTextResponse("Internal Server Error", status_code=500)


def _htmx_error(request: Request, text: str, status_code: int) -> HTMLResponse:
    # A failed poll's text goes to #poll-error, which the next good poll clears.
    poll = request.url.path.endswith("/jobs-fragment")
    headers = {"HX-Retarget": "#poll-error"} if poll else None
    return HTMLResponse(html.escape(text), status_code=status_code, headers=headers)


app.add_middleware(SlowAPIMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
# Last added = outermost: oversized bodies are refused before anything else reads them.
app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_REQUEST_BYTES)
# Outermost of all, so even a 413 answer carries the request id.
app.add_middleware(RequestContextMiddleware)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(manuscripts.router)
app.include_router(billing.router)
app.include_router(web.public_router)
app.include_router(web.router)

# The Windows registry can map .js to text/plain, which nosniff then refuses to run.
mimetypes.add_type("text/javascript", ".js")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
