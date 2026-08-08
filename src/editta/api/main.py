"""FastAPI app factory: composition root wiring routers, middleware, rate limiting."""

from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog
from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from editta.api.middleware import SecurityHeadersMiddleware, limiter
from editta.api.routes import auth, billing, manuscripts, web

logger = structlog.get_logger(__name__)


def _scrub_manuscript_content(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    """Never let manuscript text reach logs, even by accident (PLAN.md §5)."""
    for key in ("text", "manuscript_block", "content"):
        event_dict.pop(key, None)
    return event_dict


structlog.configure(
    processors=[_scrub_manuscript_content, structlog.processors.JSONRenderer()],
)

app = FastAPI(title="Editta API")
app.state.limiter = limiter
# slowapi's handler is typed for RateLimitExceeded specifically; Starlette's stub
# wants a general Exception handler — safe in practice, Starlette dispatches by the
# exact registered type.
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(auth.router)
app.include_router(manuscripts.router)
app.include_router(billing.router)
app.include_router(web.router)
