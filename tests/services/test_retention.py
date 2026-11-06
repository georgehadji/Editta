from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from editta.services.jobs import _pass_checkpoint_key, _report_key
from editta.services.retention import RetentionRepositories, purge_expired
from tests.services.fakes import (
    FakeAnalysisJobRepo,
    FakeBlobStore,
    FakeManuscriptRepo,
    FakePassResultRepo,
    FakeReportRepo,
    _PassResultRecord,
    _ReportRecord,
)

NOW = datetime(2026, 6, 1, tzinfo=UTC)


class World:
    def __init__(self) -> None:
        self.manuscripts = FakeManuscriptRepo()
        self.jobs = FakeAnalysisJobRepo()
        self.pass_results = FakePassResultRepo()
        self.reports = FakeReportRepo()
        self.blobs = FakeBlobStore()

    def uow(self):  # noqa: ANN201
        @asynccontextmanager
        async def unit():  # noqa: ANN202
            yield RetentionRepositories(
                manuscripts=self.manuscripts,  # type: ignore[arg-type]
                jobs=self.jobs,  # type: ignore[arg-type]
                pass_results=self.pass_results,  # type: ignore[arg-type]
                reports=self.reports,  # type: ignore[arg-type]
            )

        return unit

    def analysed_manuscript(self, *, expires: datetime) -> tuple:  # type: ignore[type-arg]
        tenant_id, manuscript_id = uuid4(), uuid4()
        source = f"{tenant_id}/manuscripts/{manuscript_id}/source"
        self.manuscripts.seed(
            tenant_id, manuscript_id, blob_key=source, title="t", retention_until=expires
        )
        job = self.jobs.seed(tenant_id, uuid4(), manuscript_id, lease_owner=None)
        checkpoint = _pass_checkpoint_key(job.id, "language")
        synthesis = _pass_checkpoint_key(job.id, "synthesis")
        report = _report_key(job.id)
        for key in (source, checkpoint, synthesis, report):
            self.blobs.seed(key, b"quotes from the manuscript")
        self.pass_results.seed(_PassResultRecord(job.id, "language", checkpoint, "m", {}))
        self.reports._records[job.id] = _ReportRecord(job.id, report)
        return tenant_id, manuscript_id, job.id, source, checkpoint, synthesis, report


@pytest.mark.asyncio
async def test_purge_removes_source_checkpoints_and_report_but_keeps_the_record() -> None:
    world = World()
    tenant_id, manuscript_id, job_id, source, checkpoint, synthesis, report = (
        world.analysed_manuscript(expires=NOW - timedelta(days=1))
    )

    result = await purge_expired(world.uow(), world.blobs, now=NOW)

    assert (result.purged, result.failed) == (1, 0)
    assert source not in world.blobs._data
    assert checkpoint not in world.blobs._data and synthesis not in world.blobs._data
    # The report quotes the manuscript too, and nothing else would ever delete it.
    assert report not in world.blobs._data
    assert await world.reports.get_by_job_id(job_id) is None
    assert await world.pass_results.list_for_job(job_id) == []
    record = await world.manuscripts.get_by_id(tenant_id, manuscript_id)
    assert record is not None and record.status == "purged" and record.blob_key == ""


@pytest.mark.asyncio
async def test_unexpired_manuscripts_are_untouched_and_purge_is_idempotent() -> None:
    world = World()
    world.analysed_manuscript(expires=NOW - timedelta(days=1))
    *_, fresh_source, _, _, _ = world.analysed_manuscript(expires=NOW + timedelta(days=1))

    first = await purge_expired(world.uow(), world.blobs, now=NOW)
    second = await purge_expired(world.uow(), world.blobs, now=NOW)

    assert (first.purged, second.purged) == (1, 0)
    assert fresh_source in world.blobs._data


@pytest.mark.asyncio
async def test_a_failure_leaves_the_manuscript_for_the_next_sweep() -> None:
    """Storage first, record last: a failure must never mark content purged that
    still exists."""

    class FlakyBlobs(FakeBlobStore):
        fail = True

        async def delete_prefix(self, prefix: str) -> None:
            if self.fail and prefix.startswith("jobs/"):
                raise OSError("object store unavailable")
            await super().delete_prefix(prefix)

    world = World()
    world.blobs = FlakyBlobs()
    tenant_id, manuscript_id, *_ = world.analysed_manuscript(expires=NOW - timedelta(days=1))

    failed = await purge_expired(world.uow(), world.blobs, now=NOW)
    world.blobs.fail = False  # type: ignore[attr-defined]
    retried = await purge_expired(world.uow(), world.blobs, now=NOW)

    assert (failed.purged, failed.failed) == (0, 1)
    assert retried.purged == 1
    record = await world.manuscripts.get_by_id(tenant_id, manuscript_id)
    assert record is not None and record.status == "purged"
