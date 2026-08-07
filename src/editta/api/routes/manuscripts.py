"""Upload, job status polling, report download via presigned URL."""

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status

from editta.adapters.parsing.safety import validate_upload
from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    AuditEventRepository,
    ManuscriptRepository,
    ReportRepository,
)
from editta.api.deps import (
    AuthContext,
    get_audit_repo,
    get_blob_store,
    get_current_auth,
    get_job_repo,
    get_manuscript_repo,
    get_report_repo,
    get_settings,
)
from editta.api.middleware import verify_csrf
from editta.api.schemas import (
    Envelope,
    JobStatusResponse,
    ManuscriptUploadResponse,
    ReportDownloadResponse,
)
from editta.config import Settings
from editta.domain.errors import ParseError
from editta.domain.ports import BlobStore

router = APIRouter(
    prefix="/manuscripts", tags=["manuscripts"], dependencies=[Depends(verify_csrf)]
)

REPORT_URL_EXPIRES_SECONDS = 3600


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_manuscript(
    file: UploadFile,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    manuscripts: Annotated[ManuscriptRepository, Depends(get_manuscript_repo)],
    jobs: Annotated[AnalysisJobRepository, Depends(get_job_repo)],
    audit: Annotated[AuditEventRepository, Depends(get_audit_repo)],
    blobs: Annotated[BlobStore, Depends(get_blob_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Envelope[ManuscriptUploadResponse]:
    data = await file.read()
    filename = file.filename or "manuscript.docx"
    try:
        validate_upload(data, filename)
    except ParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    sha256 = hashlib.sha256(data).hexdigest()
    retention_until = datetime.now(UTC) + timedelta(days=settings.manuscript_retention_days)

    manuscript = await manuscripts.create(
        tenant_id=auth.tenant_id,
        title=filename,
        blob_key=f"{auth.tenant_id}/manuscripts/{sha256}/{filename}",
        sha256=sha256,
        retention_until=retention_until,
    )
    await blobs.put(manuscript.blob_key, _single_chunk(data))

    job = await jobs.create(tenant_id=auth.tenant_id, manuscript_id=manuscript.id)
    await audit.append(
        tenant_id=auth.tenant_id, actor=auth.email, action="upload", subject=str(manuscript.id)
    )

    return Envelope.ok(ManuscriptUploadResponse(manuscript_id=manuscript.id, job_id=job.id))


@router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: UUID,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    jobs: Annotated[AnalysisJobRepository, Depends(get_job_repo)],
) -> Envelope[JobStatusResponse]:
    job = await jobs.get_by_id(auth.tenant_id, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return Envelope.ok(JobStatusResponse(job_id=job.id, state=job.state, error=job.error))


@router.get("/jobs/{job_id}/report")
async def get_report_download_url(
    job_id: UUID,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    jobs: Annotated[AnalysisJobRepository, Depends(get_job_repo)],
    reports: Annotated[ReportRepository, Depends(get_report_repo)],
    audit: Annotated[AuditEventRepository, Depends(get_audit_repo)],
    blobs: Annotated[BlobStore, Depends(get_blob_store)],
) -> Envelope[ReportDownloadResponse]:
    # Report rows have no tenant_id of their own (PLAN.md §3) — authorize via the job.
    job = await jobs.get_by_id(auth.tenant_id, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")

    report = await reports.get_by_job_id(job_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not ready")

    url = await blobs.presigned_get_url(report.blob_key, expires_in=REPORT_URL_EXPIRES_SECONDS)
    await audit.append(
        tenant_id=auth.tenant_id, actor=auth.email, action="download", subject=str(report.id)
    )
    return Envelope.ok(ReportDownloadResponse(url=url, expires_in=REPORT_URL_EXPIRES_SECONDS))


async def _single_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data
