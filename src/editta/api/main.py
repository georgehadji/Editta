"""FastAPI app factory: composition root wiring routers, middleware, rate limiting."""

import logging
import re

import structlog
from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from editta.api.middleware import (
    MAX_REQUEST_BYTES,
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    limiter,
)
from editta.api.routes import auth, billing, manuscripts, web
from editta.observability import configure_logging

logger = structlog.get_logger(__name__)

_SECRET_QUERY = re.compile(r"(?<=[?&]token=)[^&\s]+")


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
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
# Last added = outermost: oversized bodies are refused before anything else reads them.
app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_REQUEST_BYTES)
# Outermost of all, so even a 413 answer carries the request id.
app.add_middleware(RequestContextMiddleware)

app.include_router(auth.router)
app.include_router(manuscripts.router)
app.include_router(billing.router)
app.include_router(web.public_router)
app.include_router(web.router)
