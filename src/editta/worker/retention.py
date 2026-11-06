"""Retention sweep entrypoint: purges manuscript content past retention_until
(services/retention.py has the what and why; this is wiring).

Run on a schedule (cron / systemd timer) — unlike worker/main.py this doesn't loop
forever. Manuscript rows carry no per-request tenant context here (the sweep is
cross-tenant by nature), so — like the job queue — it needs the BYPASSRLS
`editta_worker` role (infra/migrations 0002-0003), not the RLS-scoped app role.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    ManuscriptRepository,
    PassResultRepository,
    ReportRepository,
)
from editta.adapters.persistence.roles import require_rls_bypass
from editta.adapters.storage.s3 import S3BlobStore
from editta.config import load_settings, worker_database_url
from editta.services.retention import (
    RetentionRepositories,
    RetentionUnitOfWork,
    SweepResult,
    purge_expired,
)

logger = structlog.get_logger(__name__)


def retention_unit_of_work(
    sessions: async_sessionmaker[AsyncSession],
) -> RetentionUnitOfWork:
    @asynccontextmanager
    async def unit() -> AsyncIterator[RetentionRepositories]:
        async with sessions() as session, session.begin():
            yield RetentionRepositories(
                manuscripts=ManuscriptRepository(session),
                jobs=AnalysisJobRepository(session),
                pass_results=PassResultRepository(session),
                reports=ReportRepository(session),
            )

    return unit


async def _main_async() -> SweepResult:
    settings = load_settings()
    engine = create_async_engine(worker_database_url(settings))
    await require_rls_bypass(engine, purpose="the retention sweep")
    blobs = S3BlobStore(
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
    )
    try:
        return await purge_expired(
            retention_unit_of_work(async_sessionmaker(engine, expire_on_commit=False)), blobs
        )
    finally:
        await engine.dispose()


def main() -> None:
    structlog.configure(processors=[structlog.processors.JSONRenderer()])
    result = asyncio.run(_main_async())
    print(f"retention sweep: {result.purged} purged, {result.failed} failed")
    if result.failed:
        raise SystemExit(1)  # make a failing sweep visible to cron / the scheduler


if __name__ == "__main__":
    main()
