"""Manuscript submission: validate -> pay -> store under a UUID key -> create job -> audit.

Credits are enforced here, at upload, not when the worker starts the job: a tenant
with too few credits gets InsufficientCredits (HTTP 402) and nothing is stored or
queued (the already-stored blob is deleted again). The tenant row is locked for the
rest of the request transaction, so the balance check and the reservation are atomic —
two concurrent uploads cannot both spend the last credit. The blob is stored before
the lock is taken, so the lock is never held across the upload to object storage.
The reservation uses the same idempotency key the job runner refunds against when a
job dies.

Shared by the JSON API and the web UI, which previously each carried their own copy.
Storage keys are opaque UUIDs (PLAN.md §5). The old content-addressed key
`{tenant}/manuscripts/{sha256}/{filename}` made two uploads of the same file share one
blob, so the retention sweep deleting the first manuscript also deleted the blob the
second still pointed at — and it put client-supplied names into object-store paths.
The sanitized original filename is kept only as DB metadata.
"""

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from editta.adapters.parsing import sanitize_filename, validate_upload
from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    AuditEventRepository,
    CreditLedgerRepository,
    ManuscriptRepository,
    TenantRepository,
)
from editta.domain.credits import DEFAULT_JOB_COST_CREDITS, require_credits, reservation_key
from editta.domain.errors import EdittaError
from editta.domain.ports import BlobStore


@dataclass(frozen=True, slots=True)
class SubmittedManuscript:
    manuscript_id: UUID
    job_id: UUID


def manuscript_blob_key(tenant_id: UUID, object_id: UUID) -> str:
    return f"{tenant_id}/manuscripts/{object_id}/source"


async def _single_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def submit_manuscript(
    *,
    tenant_id: UUID,
    actor: str,
    data: bytes,
    filename: str | None,
    manuscripts: ManuscriptRepository,
    jobs: AnalysisJobRepository,
    audit: AuditEventRepository,
    tenants: TenantRepository,
    ledger: CreditLedgerRepository,
    blobs: BlobStore,
    retention_days: int,
    job_cost_credits: int = DEFAULT_JOB_COST_CREDITS,
    now: datetime | None = None,
) -> SubmittedManuscript:
    """All repositories must share the caller's transaction (the lock lasts until it
    commits). Raises ParseError for a rejected file and InsufficientCredits when the
    tenant cannot pay; nothing is stored in either case."""
    title = sanitize_filename(filename or "manuscript.docx")
    validate_upload(data, title)

    # Store first, lock second: the upload to object storage is the slow part, and
    # the lock should be held for milliseconds, not for the whole transfer.
    blob_key = manuscript_blob_key(tenant_id, uuid4())
    await blobs.put(blob_key, _single_chunk(data))
    try:
        if not await tenants.lock(tenant_id):
            raise EdittaError(f"tenant {tenant_id} not found")
        require_credits(await ledger.balance(tenant_id), job_cost_credits)
        manuscript = await manuscripts.create(
            tenant_id=tenant_id,
            title=title,
            blob_key=blob_key,
            sha256=hashlib.sha256(data).hexdigest(),
            retention_until=(now or datetime.now(UTC)) + timedelta(days=retention_days),
        )
        job = await jobs.create(tenant_id=tenant_id, manuscript_id=manuscript.id)
        await ledger.append_once(
            tenant_id=tenant_id,
            delta=-job_cost_credits,
            reason="job",
            idempotency_key=reservation_key(job.id),
            job_id=job.id,
        )
        await audit.append(
            tenant_id=tenant_id, actor=actor, action="upload", subject=str(manuscript.id)
        )
    except Exception:
        # Refused or failed: no row will point at the blob, so the retention sweep could
        # never delete it. Keep nothing of the manuscript.
        await blobs.delete_prefix(blob_key)
        raise
    return SubmittedManuscript(manuscript_id=manuscript.id, job_id=job.id)
