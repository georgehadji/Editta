"""Integration tests against real Postgres via testcontainers.

Verifies RLS cross-tenant isolation and SKIP LOCKED job-claim exclusivity — the two
guarantees that are meaningless to test against fakes, because fakes don't have a
database to leak across — plus the transactional behaviour the job runner depends on
(per-step commits, idempotent ledger inserts, retry backoff via the lease).

Runs against a throwaway database: either a testcontainers Postgres (needs a Docker
daemon) or, when EDITTA_TEST_DATABASE_URL points at a server you can CREATE DATABASE
on, a fresh database created and dropped per test. Skips cleanly with neither.
"""

import asyncio
import io
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import docx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    CreditLedgerRepository,
    ManuscriptRepository,
    PassResultRepository,
    ReportRepository,
    TenantRepository,
)
from editta.adapters.persistence.tables import Base
from editta.adapters.persistence.uow import UnitOfWork
from editta.adapters.queue.postgres import PostgresJobQueue
from editta.adapters.storage.local import LocalBlobStore
from editta.domain.errors import LLMError
from editta.domain.models import ClaimedJob, JobOutcomeKind, PromptParts
from editta.domain.states import JobState
from editta.services.jobs import JobRunner
from editta.worker.main import job_unit_of_work
from tests.fakes import FakeLLMClient

_RLS_TABLES = ("users", "manuscripts", "analysis_jobs", "credit_ledger", "audit_events")


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=5, check=True)  # noqa: S607
    except Exception:
        return False
    return True


_EXTERNAL_DB_URL = os.environ.get("EDITTA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _EXTERNAL_DB_URL and not _docker_available(),
    reason="neither EDITTA_TEST_DATABASE_URL nor a Docker daemon is available",
)


@contextmanager
def _testcontainers_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql+asyncpg")


async def _create_scratch_db(admin_url: str) -> tuple[str, str]:
    name = f"editta_test_{uuid4().hex[:12]}"
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()
    return name, admin_url.rsplit("/", 1)[0] + f"/{name}"


async def _drop_scratch_db(admin_url: str, name: str) -> None:
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await admin.dispose()


@pytest_asyncio.fixture
async def pg_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if _EXTERNAL_DB_URL:
        name, url = await _create_scratch_db(_EXTERNAL_DB_URL)
        try:
            async for factory in _schema_and_factory(url):
                yield factory
        finally:
            await _drop_scratch_db(_EXTERNAL_DB_URL, name)
    else:
        with _testcontainers_url() as url:
            async for factory in _schema_and_factory(url):
                yield factory


async def _schema_and_factory(url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table in _RLS_TABLES:
            await conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            await conn.execute(
                text(f"""
                    CREATE POLICY tenant_isolation ON {table}
                    USING (
                        tenant_id = current_setting('app.current_tenant_id', true)::uuid
                    )
                    WITH CHECK (
                        tenant_id = current_setting('app.current_tenant_id', true)::uuid
                    )
                """)
            )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_rls_blocks_cross_tenant_manuscript_reads(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    uow = UnitOfWork(pg_session_factory)

    async with pg_session_factory() as setup_session:  # tenants: no RLS
        tenant_a = (await TenantRepository(setup_session).create(name="Tenant A")).id
        tenant_b = (await TenantRepository(setup_session).create(name="Tenant B")).id
        await setup_session.commit()

    async with uow.begin(tenant_a) as session:
        manuscript = await ManuscriptRepository(session).create(
            tenant_id=tenant_a,
            title="secret.docx",
            blob_key="k",
            sha256="h",
            retention_until=datetime.now(UTC),
        )

    async with uow.begin(tenant_b) as session:
        # No missing WHERE clause here — get_by_id already filters tenant_id=tenant_b.
        # The point is RLS still hides it even if that filter had been forgotten.
        leaked = await ManuscriptRepository(session).get_by_id(tenant_b, manuscript.id)
        assert leaked is None

    async with uow.begin(tenant_a) as session:
        visible = await ManuscriptRepository(session).get_by_id(tenant_a, manuscript.id)
        assert visible is not None


@pytest.mark.asyncio
async def test_skip_locked_claim_never_double_claims_under_concurrency(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        tenant_id = (await TenantRepository(session).create(name="Tenant")).id
        manuscript = await ManuscriptRepository(session).create(
            tenant_id=tenant_id,
            title="m.docx",
            blob_key="k",
            sha256="h",
            retention_until=datetime.now(UTC),
        )
        job = await AnalysisJobRepository(session).create(
            tenant_id=tenant_id, manuscript_id=manuscript.id
        )
        await session.commit()

    async def _claim(worker_id: str) -> object:
        async with pg_session_factory() as session:
            return await PostgresJobQueue(session).claim(worker_id=worker_id)

    results = await asyncio.gather(_claim("worker-1"), _claim("worker-2"))
    claimed = [r for r in results if r is not None]

    assert len(claimed) == 1
    assert claimed[0].job_id == job.id


async def _seed_job(
    factory: async_sessionmaker[AsyncSession], *, blob_key: str = "k", title: str = "m.docx"
) -> tuple[UUID, UUID, UUID]:
    async with factory() as session:
        tenant_id = (await TenantRepository(session).create(name="Tenant")).id
        manuscript = await ManuscriptRepository(session).create(
            tenant_id=tenant_id,
            title=title,
            blob_key=blob_key,
            sha256="h",
            retention_until=datetime.now(UTC),
        )
        job = await AnalysisJobRepository(session).create(
            tenant_id=tenant_id, manuscript_id=manuscript.id
        )
        await session.commit()
    return tenant_id, manuscript.id, job.id


@pytest.mark.asyncio
async def test_ledger_append_once_is_idempotent_inside_one_transaction(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        tenant_id = (await TenantRepository(session).create(name="Tenant")).id
        await session.commit()

    uow = UnitOfWork(pg_session_factory)
    # A duplicate key must not poison the transaction: the old suppress(IntegrityError)
    # pattern left it aborted, so the commit at the end of this block raised.
    async with uow.begin(tenant_id) as session:
        ledger = CreditLedgerRepository(session)
        first = await ledger.append_once(
            tenant_id=tenant_id, delta=10, reason="purchase", idempotency_key="stripe:evt_1"
        )
        replay = await ledger.append_once(
            tenant_id=tenant_id, delta=10, reason="purchase", idempotency_key="stripe:evt_1"
        )
        assert (first, replay) == (True, False)
        assert await ledger.has_entry("stripe:evt_1")

    async with uow.begin(tenant_id) as session:
        assert await CreditLedgerRepository(session).balance(tenant_id) == 10


@pytest.mark.asyncio
async def test_deferred_job_is_not_claimable_until_its_backoff_elapses(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, _, job_id = await _seed_job(pg_session_factory)

    async with pg_session_factory() as session:
        queue = PostgresJobQueue(session)
        assert (await queue.claim(worker_id="w1")) is not None
        await queue.defer(job_id, delay_seconds=3600)
        assert await queue.claim(worker_id="w2") is None  # backing off

        await queue.defer(job_id, delay_seconds=0)
        await asyncio.sleep(0.01)
        reclaimed = await queue.claim(worker_id="w2")
        assert reclaimed is not None and reclaimed.job_id == job_id


@pytest.mark.asyncio
async def test_dead_job_is_never_claimed_again(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _, job_id = await _seed_job(pg_session_factory)

    async with UnitOfWork(pg_session_factory).begin(tenant_id) as session:
        await AnalysisJobRepository(session).record_failure(
            tenant_id, job_id, state=JobState.DEAD, error="boom", attempts_made=3
        )

    async with pg_session_factory() as session:
        assert await PostgresJobQueue(session).claim(worker_id="w") is None


@pytest.mark.asyncio
async def test_report_create_is_idempotent_per_job(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _, job_id = await _seed_job(pg_session_factory)
    uow = UnitOfWork(pg_session_factory)

    for key in ("jobs/a/report.docx", "jobs/b/report.docx"):
        async with uow.begin(tenant_id) as session:
            await ReportRepository(session).create(job_id=job_id, blob_key=key)

    async with uow.begin(tenant_id) as session:
        report = await ReportRepository(session).get_by_job_id(job_id)
        assert report is not None and report.blob_key == "jobs/b/report.docx"


@pytest.mark.asyncio
async def test_failed_attempt_keeps_checkpoints_and_retry_completes_on_real_db(
    pg_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """End-to-end regression for the single-transaction bug, on real Postgres: a pass
    failure must leave completed checkpoints, the attempt count and the retry state
    committed, and the retry must finish the job without re-running those passes."""
    blobs = LocalBlobStore(tmp_path)
    await blobs.put("raw/essay", _chunk(_docx_bytes(MANUSCRIPT_TEXT)))
    tenant_id, manuscript_id, job_id = await _seed_job(
        pg_session_factory, blob_key="raw/essay", title="essay.docx"
    )
    failing = {"active": True}

    def rule(parts: PromptParts) -> BaseException | None:
        if failing["active"] and parts.instructions.startswith("Analyze structure"):
            return LLMError("overloaded")
        return None

    llm = FakeLLMClient(manuscript_text=MANUSCRIPT_TEXT, fail_when=rule)
    uow = UnitOfWork(pg_session_factory)
    runner = JobRunner(
        uow=job_unit_of_work(uow, tenant_id), blobs=blobs, llm=llm, cost_ceiling_usd=6.0
    )
    claimed = ClaimedJob(job_id=job_id, tenant_id=tenant_id, manuscript_id=manuscript_id, attempt=0)

    first = await runner.run(claimed)

    assert first.kind is JobOutcomeKind.RETRY
    async with uow.begin(tenant_id) as session:
        job = await AnalysisJobRepository(session).get_by_id(tenant_id, job_id)
        rows = await PassResultRepository(session).list_for_job(job_id)
        ledger = CreditLedgerRepository(session)
        assert job is not None and (job.state, job.attempt) == (JobState.PENDING, 1)
        assert {r.pass_name for r in rows} == {"language", "metadata", "integrity"}
        assert await ledger.has_entry(f"job:{job_id}:reserve")
        assert not await ledger.has_entry(f"job:{job_id}:refund")

    failing["active"] = False
    calls_before = len(llm.calls)
    second = await runner.run(claimed.model_copy(update={"attempt": 1}))

    assert second.kind is JobOutcomeKind.COMPLETED
    assert len(llm.calls) - calls_before == 2  # structure + synthesis
    async with uow.begin(tenant_id) as session:
        job = await AnalysisJobRepository(session).get_by_id(tenant_id, job_id)
        assert job is not None and job.state == JobState.COMPLETED
        assert await ReportRepository(session).get_by_job_id(job_id) is not None
        assert await CreditLedgerRepository(session).balance(tenant_id) == -1


MANUSCRIPT_TEXT = "This is a Greek essay about language and structure and much more content here."


def _docx_bytes(text: str) -> bytes:
    document = docx.Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


async def _chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data
