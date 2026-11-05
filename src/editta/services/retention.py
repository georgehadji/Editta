"""Retention: purge manuscript content past `retention_until` (PLAN.md §5, GDPR).

What is purged: the uploaded source file, every analysis checkpoint (pass findings and
the synthesis — they quote the manuscript), and their database rows. What stays: the
manuscript row itself (marked `purged`, no storage key), its jobs, and the final reports,
which PLAN.md keeps until the tenant deletes them.

The previous sweep tried to delete the manuscript row, which jobs reference by foreign
key, so it failed on every manuscript that had ever been analysed — after it had already
deleted the source file — and it never touched the checkpoints at all.

Ordering is chosen so a failure can only ever leave work for the next run, never leave
content behind: storage objects are deleted first (idempotent), and only then does one
transaction delete the checkpoint rows and mark the manuscript purged. Each manuscript is
purged independently; one failure is logged and does not stop the others.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog

from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    ManuscriptRepository,
    PassResultRepository,
)
from editta.domain.ports import BlobStore

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RetentionRepositories:
    manuscripts: ManuscriptRepository
    jobs: AnalysisJobRepository
    pass_results: PassResultRepository


# One transaction across tenants — the sweep runs on the BYPASSRLS worker role.
RetentionUnitOfWork = Callable[[], AbstractAsyncContextManager[RetentionRepositories]]


@dataclass(frozen=True, slots=True)
class SweepResult:
    purged: int
    failed: int


def job_checkpoint_prefix(job_id: UUID) -> str:
    """Pass and synthesis checkpoints (services/jobs.py) — not the report beside them."""
    return f"jobs/{job_id}/passes/"


async def purge_expired(
    uow: RetentionUnitOfWork, blobs: BlobStore, *, now: datetime | None = None
) -> SweepResult:
    async with uow() as repos:
        expired = [
            (m.tenant_id, m.id, m.blob_key)
            for m in await repos.manuscripts.list_expired(now or datetime.now(UTC))
        ]

    purged = failed = 0
    for tenant_id, manuscript_id, blob_key in expired:
        try:
            await _purge_one(uow, blobs, tenant_id, manuscript_id, blob_key)
        except Exception:
            failed += 1
            logger.exception("retention.purge_failed", manuscript_id=str(manuscript_id))
        else:
            purged += 1
    logger.info("retention.swept", purged=purged, failed=failed)
    return SweepResult(purged=purged, failed=failed)


async def _purge_one(
    uow: RetentionUnitOfWork,
    blobs: BlobStore,
    tenant_id: UUID,
    manuscript_id: UUID,
    blob_key: str,
) -> None:
    async with uow() as repos:
        job_ids = await repos.jobs.list_ids_for_manuscript(tenant_id, manuscript_id)

    # Storage first: if anything below fails, the manuscript is still "expired, not
    # purged" and the next sweep repeats these (idempotent) deletes.
    if blob_key:
        await blobs.delete_prefix(blob_key)
    for job_id in job_ids:
        await blobs.delete_prefix(job_checkpoint_prefix(job_id))

    async with uow() as repos:
        await repos.pass_results.delete_for_jobs(job_ids)
        await repos.manuscripts.mark_purged(tenant_id, manuscript_id)
