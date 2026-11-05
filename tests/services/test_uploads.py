import io
from uuid import uuid4

import docx
import pytest

from editta.domain.errors import ParseError
from editta.services.uploads import submit_manuscript
from tests.services.fakes import (
    FakeAnalysisJobRepo,
    FakeAuditRepo,
    FakeBlobStore,
    FakeManuscriptRepo,
)


def _docx() -> bytes:
    document = docx.Document()
    document.add_paragraph("body")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class Deps:
    def __init__(self) -> None:
        self.manuscripts = FakeManuscriptRepo()
        self.jobs = FakeAnalysisJobRepo()
        self.audit = FakeAuditRepo()
        self.blobs = FakeBlobStore()

    async def submit(self, data: bytes, filename: str | None) -> object:
        return await submit_manuscript(
            tenant_id=TENANT,
            actor="editor@example.com",
            data=data,
            filename=filename,
            manuscripts=self.manuscripts,  # type: ignore[arg-type]
            jobs=self.jobs,  # type: ignore[arg-type]
            audit=self.audit,  # type: ignore[arg-type]
            blobs=self.blobs,  # type: ignore[arg-type]
            retention_days=30,
        )


TENANT = uuid4()


@pytest.mark.asyncio
async def test_identical_uploads_get_independent_blobs() -> None:
    """Regression: content-addressed keys made two uploads share one blob, so the
    retention sweep deleting the first also destroyed the second."""
    deps = Deps()
    first = await deps.submit(_docx(), "essay.docx")
    second = await deps.submit(_docx(), "essay.docx")

    key_1 = (await deps.manuscripts.get_by_id(TENANT, first.manuscript_id)).blob_key  # type: ignore[attr-defined,union-attr]
    key_2 = (await deps.manuscripts.get_by_id(TENANT, second.manuscript_id)).blob_key  # type: ignore[attr-defined,union-attr]
    assert key_1 != key_2

    await deps.blobs.delete_prefix(key_1)  # what the retention sweep does
    assert [c async for c in deps.blobs.get(key_2)]


@pytest.mark.asyncio
async def test_storage_key_never_contains_the_client_filename() -> None:
    deps = Deps()
    submitted = await deps.submit(_docx(), "../../Μυστικό Δοκίμιο.docx")

    record = await deps.manuscripts.get_by_id(TENANT, submitted.manuscript_id)  # type: ignore[attr-defined]
    assert record is not None
    assert "Δοκίμιο" not in record.blob_key and ".." not in record.blob_key
    assert record.blob_key.startswith(f"{TENANT}/manuscripts/")
    assert record.title == "Μυστικό Δοκίμιο.docx"  # sanitized, kept as metadata


@pytest.mark.asyncio
async def test_rejected_file_stores_nothing() -> None:
    deps = Deps()
    with pytest.raises(ParseError):
        await deps.submit(b"not a docx", "essay.docx")
    assert deps.blobs._data == {}
    assert await deps.jobs.list_for_tenant(TENANT) == []


@pytest.mark.asyncio
async def test_submission_creates_job_and_audit_event() -> None:
    deps = Deps()
    submitted = await deps.submit(_docx(), None)

    jobs = await deps.jobs.list_for_tenant(TENANT)
    assert [j.id for j in jobs] == [submitted.job_id]  # type: ignore[attr-defined]
    assert deps.audit.entries[0]["action"] == "upload"
