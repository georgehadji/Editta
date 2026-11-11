"""Liveness and readiness. Unauthenticated, exempt from rate limits, and they say nothing
about the deployment: no version, no settings, no error text."""

from typing import Annotated, Protocol

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from editta.api.deps import get_uow
from editta.api.middleware import limiter

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


class _Pingable(Protocol):
    async def ping(self) -> None: ...


# slowapi's `exempt` is untyped; the default 120/minute limit would throttle a probe.
@router.get("/healthz")
@limiter.exempt  # type: ignore[untyped-decorator]
async def liveness() -> dict[str, str]:
    """The process is up and the event loop answers. Touches nothing else, so a database
    outage does not make an orchestrator restart a healthy process."""
    return {"status": "ok"}


@router.get("/readyz")
@limiter.exempt  # type: ignore[untyped-decorator]
async def readiness(database: Annotated[_Pingable, Depends(get_uow)]) -> JSONResponse:
    """Can it serve: the database answers."""
    try:
        await database.ping()
    except Exception:
        logger.warning("readyz.database_unreachable")
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})
