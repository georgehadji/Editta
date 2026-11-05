"""Report download: the one place that decides who may fetch a report and for how long.

Shared by the JSON API and the web UI, which each carried their own copy. Delivery
layers only translate the outcome: a JSON envelope, or a redirect.
"""

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    AuditEventRepository,
    ReportRepository,
)
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
    actor: str,
    jobs: AnalysisJobRepository,
    reports: ReportRepository,
    audit: AuditEventRepository,
    blobs: BlobStore,
) -> ReportDownload:
    """A presigned URL for the job's report, audited. Raises JobNotFound / ReportNotReady."""
    # Report rows have no tenant_id of their own (PLAN.md §3) -- authorize via the job.
    if await jobs.get_by_id(tenant_id, job_id) is None:
        raise JobNotFound
    report = await reports.get_by_job_id(job_id)
    if report is None:
        raise ReportNotReady
    url = await blobs.presigned_get_url(report.blob_key, expires_in=REPORT_URL_EXPIRES_SECONDS)
    await audit.append(tenant_id=tenant_id, actor=actor, action="download", subject=str(report.id))
    return ReportDownload(url=url, expires_in=REPORT_URL_EXPIRES_SECONDS)
