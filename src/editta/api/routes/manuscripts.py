"""Upload, job status polling, report download via presigned URL."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status

from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    AuditEventRepository,
    CreditLedgerRepository,
    ManuscriptRepository,
    ReportRepository,
    TenantRepository,
)
from editta.api.deps import (
    AuthContext,
    get_audit_repo,
    get_blob_store,
    get_current_auth,
    get_job_repo,
    get_ledger_repo,
    get_manuscript_repo,
    get_report_repo,
    get_settings,
    get_tenant_repo,
)
from editta.api.middleware import verify_csrf
from editta.api.schemas import (
    Envelope,
    JobStatusResponse,
    ManuscriptUploadResponse,
    ReportDownloadResponse,
)
from editta.config import Settings
from editta.domain.artifacts import ArtifactKind
from editta.domain.errors import InsufficientCredits, NotFound, ParseError
from editta.domain.ports import BlobStore
from editta.services.reports import report_download
from editta.services.uploads import submit_manuscript

router = APIRouter(prefix="/manuscripts", tags=["manuscripts"], dependencies=[Depends(verify_csrf)])


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_manuscript(
    file: UploadFile,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    manuscripts: Annotated[ManuscriptRepository, Depends(get_manuscript_repo)],
    jobs: Annotated[AnalysisJobRepository, Depends(get_job_repo)],
    audit: Annotated[AuditEventRepository, Depends(get_audit_repo)],
    tenants: Annotated[TenantRepository, Depends(get_tenant_repo)],
    ledger: Annotated[CreditLedgerRepository, Depends(get_ledger_repo)],
    blobs: Annotated[BlobStore, Depends(get_blob_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Envelope[ManuscriptUploadResponse]:
    try:
        submitted = await submit_manuscript(
            tenant_id=auth.tenant_id,
            actor=auth.email,
            data=await file.read(),
            filename=file.filename,
            manuscripts=manuscripts,
            jobs=jobs,
            audit=audit,
            tenants=tenants,
            ledger=ledger,
            blobs=blobs,
            retention_days=settings.manuscript_retention_days,
            job_cost_credits=settings.job_cost_credits,
        )
    except ParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except InsufficientCredits as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
    return Envelope.ok(
        ManuscriptUploadResponse(manuscript_id=submitted.manuscript_id, job_id=submitted.job_id)
    )


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


@router.get("/jobs/{job_id}/artifacts/{kind}")
async def get_artifact_download_url(
    job_id: UUID,
    kind: ArtifactKind,  # anything but a known kind is a 422 before any lookup
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    jobs: Annotated[AnalysisJobRepository, Depends(get_job_repo)],
    manuscripts: Annotated[ManuscriptRepository, Depends(get_manuscript_repo)],
    reports: Annotated[ReportRepository, Depends(get_report_repo)],
    audit: Annotated[AuditEventRepository, Depends(get_audit_repo)],
    blobs: Annotated[BlobStore, Depends(get_blob_store)],
) -> Envelope[ReportDownloadResponse]:
    try:
        download = await report_download(
            tenant_id=auth.tenant_id,
            job_id=job_id,
            kind=kind,
            actor=auth.email,
            jobs=jobs,
            manuscripts=manuscripts,
            reports=reports,
            audit=audit,
            blobs=blobs,
        )
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return Envelope.ok(ReportDownloadResponse(url=download.url, expires_in=download.expires_in))


@router.get("/jobs/{job_id}/report")
async def get_report_download_url(
    job_id: UUID,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    jobs: Annotated[AnalysisJobRepository, Depends(get_job_repo)],
    manuscripts: Annotated[ManuscriptRepository, Depends(get_manuscript_repo)],
    reports: Annotated[ReportRepository, Depends(get_report_repo)],
    audit: Annotated[AuditEventRepository, Depends(get_audit_repo)],
    blobs: Annotated[BlobStore, Depends(get_blob_store)],
) -> Envelope[ReportDownloadResponse]:
    """Kept for existing clients: the report is one artifact kind among several."""
    return await get_artifact_download_url(
        job_id, ArtifactKind.REPORT, auth, jobs, manuscripts, reports, audit, blobs
    )
