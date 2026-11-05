"""Manuscript submission: validate -> store under a UUID key -> create job -> audit.

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
    ManuscriptRepository,
)
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
    blobs: BlobStore,
    retention_days: int,
    now: datetime | None = None,
) -> SubmittedManuscript:
    """Raises ParseError for a rejected file; nothing is stored in that case."""
    title = sanitize_filename(filename or "manuscript.docx")
    validate_upload(data, title)

    blob_key = manuscript_blob_key(tenant_id, uuid4())
    await blobs.put(blob_key, _single_chunk(data))
    manuscript = await manuscripts.create(
        tenant_id=tenant_id,
        title=title,
        blob_key=blob_key,
        sha256=hashlib.sha256(data).hexdigest(),
        retention_until=(now or datetime.now(UTC)) + timedelta(days=retention_days),
    )
    job = await jobs.create(tenant_id=tenant_id, manuscript_id=manuscript.id)
    await audit.append(
        tenant_id=tenant_id, actor=actor, action="upload", subject=str(manuscript.id)
    )
    return SubmittedManuscript(manuscript_id=manuscript.id, job_id=job.id)
