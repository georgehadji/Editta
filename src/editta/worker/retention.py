"""Retention sweep: hard-deletes manuscripts (blob + row) past retention_until.

Run on a schedule (cron / systemd timer) — unlike worker/main.py this doesn't loop
forever. Manuscript rows carry no per-request tenant context here (the sweep is
cross-tenant by nature), so — like the job queue — it needs the BYPASSRLS
`editta_worker` role (infra/migrations 0002-0003), not the RLS-scoped app role.
"""

import asyncio
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from editta.adapters.persistence.repositories import ManuscriptRepository
from editta.adapters.storage.s3 import S3BlobStore
from editta.config import load_settings, worker_database_url
from editta.domain.ports import BlobStore

logger = structlog.get_logger(__name__)


async def run_retention_sweep(
    manuscripts: ManuscriptRepository, blobs: BlobStore, *, now: datetime | None = None
) -> int:
    expired = await manuscripts.list_expired(now or datetime.now(UTC))
    for manuscript in expired:
        await blobs.delete_prefix(manuscript.blob_key)
        await manuscripts.delete(manuscript)
    logger.info("retention.swept", deleted=len(expired))
    return len(expired)


async def _main_async() -> int:
    settings = load_settings()
    engine = create_async_engine(worker_database_url(settings))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    blobs = S3BlobStore(
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
    )
    async with session_factory() as session:
        deleted = await run_retention_sweep(ManuscriptRepository(session), blobs)
        await session.commit()
    return deleted


def main() -> None:
    structlog.configure(processors=[structlog.processors.JSONRenderer()])
    deleted = asyncio.run(_main_async())
    print(f"retention sweep: {deleted} manuscript(s) deleted")


if __name__ == "__main__":
    main()
