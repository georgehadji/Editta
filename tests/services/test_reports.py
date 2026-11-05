from uuid import uuid4

import pytest

from editta.domain.errors import JobNotFound, ReportNotReady
from editta.services.reports import REPORT_URL_EXPIRES_SECONDS, report_download
from tests.services.fakes import FakeAnalysisJobRepo, FakeAuditRepo, FakeBlobStore, FakeReportRepo


class _Setup:
    def __init__(self) -> None:
        self.tenant_id, self.job_id, self.manuscript_id = uuid4(), uuid4(), uuid4()
        self.jobs = FakeAnalysisJobRepo()
        self.reports = FakeReportRepo()
        self.audit = FakeAuditRepo()
        self.blobs = FakeBlobStore()
        self.jobs.seed(self.tenant_id, self.job_id, self.manuscript_id, lease_owner=None)

    async def download(self, *, tenant_id: object = None) -> object:
        return await report_download(
            tenant_id=tenant_id or self.tenant_id,  # type: ignore[arg-type]
            job_id=self.job_id,
            actor="editor@example.com",
            jobs=self.jobs,  # type: ignore[arg-type]
            reports=self.reports,  # type: ignore[arg-type]
            audit=self.audit,  # type: ignore[arg-type]
            blobs=self.blobs,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_other_tenants_job_is_not_found() -> None:
    s = _Setup()
    await s.reports.create(job_id=s.job_id, blob_key="reports/r.docx")

    with pytest.raises(JobNotFound):
        await s.download(tenant_id=uuid4())
    assert s.audit.entries == []


@pytest.mark.asyncio
async def test_job_without_a_report_is_not_ready() -> None:
    s = _Setup()

    with pytest.raises(ReportNotReady):
        await s.download()
    assert s.audit.entries == []


@pytest.mark.asyncio
async def test_download_returns_a_timed_url_and_audits_it_once() -> None:
    s = _Setup()
    await s.reports.create(job_id=s.job_id, blob_key="reports/r.docx")

    result = await s.download()

    assert result.expires_in == REPORT_URL_EXPIRES_SECONDS == 3600  # type: ignore[attr-defined]
    assert result.url  # type: ignore[attr-defined]
    assert [e["action"] for e in s.audit.entries] == ["download"]
    assert s.audit.entries[0]["actor"] == "editor@example.com"
