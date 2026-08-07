from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from editta.worker.retention import run_retention_sweep
from tests.services.fakes import FakeBlobStore, FakeManuscriptRepo

NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_sweep_deletes_only_expired_manuscripts() -> None:
    manuscripts = FakeManuscriptRepo()
    blobs = FakeBlobStore()

    expired_id, fresh_id = uuid4(), uuid4()
    manuscripts.seed(
        uuid4(),
        expired_id,
        blob_key="a/expired.docx",
        title="expired",
        retention_until=NOW - timedelta(days=1),
    )
    manuscripts.seed(
        uuid4(),
        fresh_id,
        blob_key="b/fresh.docx",
        title="fresh",
        retention_until=NOW + timedelta(days=1),
    )
    blobs.seed("a/expired.docx", b"x")
    blobs.seed("b/fresh.docx", b"y")

    deleted = await run_retention_sweep(manuscripts, blobs, now=NOW)

    assert deleted == 1
    assert len(manuscripts._records) == 1
    assert "b/fresh.docx" in blobs._data
    assert "a/expired.docx" not in blobs._data


@pytest.mark.asyncio
async def test_sweep_is_noop_when_nothing_expired() -> None:
    manuscripts = FakeManuscriptRepo()
    blobs = FakeBlobStore()
    manuscripts.seed(
        uuid4(), uuid4(), blob_key="a.docx", title="a", retention_until=NOW + timedelta(days=30)
    )

    deleted = await run_retention_sweep(manuscripts, blobs, now=NOW)

    assert deleted == 0
