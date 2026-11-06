"""Artifact download: the one place that decides who may fetch a job's file and for how long.

Shared by the JSON API and the web UI, which each carried their own copy. Delivery
layers only translate the outcome: a JSON envelope, or a redirect.
"""

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    AuditEventRepository,
    ManuscriptRepository,
    ReportRepository,
)
from editta.domain.artifacts import ArtifactKind, artifact_filename
from editta.domain.errors import JobNotFound, ReportNotReady
from editta.domain.ports import BlobStore

REPORT_URL_EXPIRES_SECONDS: Final = 3600


@dataclass(frozen=True, slots=True)
class ReportDownload:
    url: str
    expires_in: int


async def report_download(
    *,
    tenant_id: UUID,
    job_id: UUID,
    kind: ArtifactKind,
    actor: str,
    jobs: AnalysisJobRepository,
    manuscripts: ManuscriptRepository,
    reports: ReportRepository,
    audit: AuditEventRepository,
    blobs: BlobStore,
) -> ReportDownload:
    """A presigned URL for one of the job's files, audited. Raises JobNotFound /
    ReportNotReady."""
    # Report rows have no tenant_id of their own (PLAN.md §3) -- authorize via the job.
    job = await jobs.get_by_id(tenant_id, job_id)
    if job is None:
        raise JobNotFound
    report = await reports.get(job_id, kind)
    if report is None:
        raise ReportNotReady
    manuscript = await manuscripts.get_by_id(tenant_id, job.manuscript_id)
    url = await blobs.presigned_get_url(
        report.blob_key,
        expires_in=REPORT_URL_EXPIRES_SECONDS,
        download_name=artifact_filename(manuscript.title if manuscript else "", kind),
    )
    await audit.append(
        tenant_id=tenant_id, actor=actor, action="download", subject=f"{kind.value}:{report.id}"
    )
    return ReportDownload(url=url, expires_in=REPORT_URL_EXPIRES_SECONDS)
