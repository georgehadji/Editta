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
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import docx
import pytest
import pytest_asyncio
from sqlalchemy import make_url, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    AuditEventRepository,
    CreditLedgerRepository,
    ManuscriptRepository,
    PassResultRepository,
    ReportRepository,
    TenantRepository,
    UserRepository,
)
from editta.adapters.persistence.roles import MisconfiguredRole, require_rls_bypass
from editta.adapters.persistence.tables import Report
from editta.adapters.persistence.uow import UnitOfWork
from editta.adapters.queue.postgres import PostgresJobQueue
from editta.adapters.storage.local import LocalBlobStore
from editta.cli import grant_credits
from editta.domain.artifacts import ArtifactKind
from editta.domain.errors import EdittaError, InsufficientCredits, LeaseLost, LLMError, ParseError
from editta.domain.models import JobOutcomeKind, PromptParts
from editta.domain.states import JobState
from editta.services.accounts import AccountService
from editta.services.jobs import JobRunner
from editta.services.retention import purge_expired
from editta.services.uploads import submit_manuscript
from editta.worker.main import job_unit_of_work
from editta.worker.retention import retention_unit_of_work
from tests.fakes import FakeLLMClient
from tests.services.fakes import FakeBlobStore


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

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROLE = "editta_test_app"
_WORKER_ROLE = "editta_worker"  # name fixed by migration 0003
_ROLE_PASSWORD = "editta-test-only"  # noqa: S105 -- throwaway local test role


@dataclass(frozen=True)
class Databases:
    """Connections shaped like production.

    `app`: a non-superuser role that *owns* the tables because it ran the migrations —
    exactly the .env.example setup, and the case where RLS is only enforced if FORCEd.
    `worker`: the BYPASSRLS role from migration 0003 that the queue and retention use.
    """

    app: async_sessionmaker[AsyncSession]
    worker: async_sessionmaker[AsyncSession]


@contextmanager
def _testcontainers_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql+asyncpg")


def _with_credentials(url: str, user: str, password: str, database: str) -> str:
    parts = make_url(url).set(username=user, password=password, database=database)
    return parts.render_as_string(hide_password=False)


async def _admin_exec(admin_url: str, *statements: str) -> None:
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        for statement in statements:
            await conn.execute(text(statement))
    await admin.dispose()


def _migrate(app_url: str, *, revision: str = "head", down: bool = False) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "infra" / "migrations"))
    previous = {k: os.environ.get(k) for k in ("EDITTA_DATABASE_URL", "EDITTA_WORKER_DB_PASSWORD")}
    os.environ["EDITTA_DATABASE_URL"] = app_url
    os.environ["EDITTA_WORKER_DB_PASSWORD"] = _ROLE_PASSWORD
    try:
        (command.downgrade if down else command.upgrade)(config, revision)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def _provision(admin_url: str) -> AsyncIterator[Databases]:
    name = f"editta_test_{uuid4().hex[:12]}"
    # Identifiers below are module constants, not input.
    role_ddl = (
        "DO $$ BEGIN "  # noqa: S608
        f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN "
        f"CREATE ROLE {_APP_ROLE} LOGIN PASSWORD '{_ROLE_PASSWORD}' NOSUPERUSER NOBYPASSRLS; "
        "END IF; "
        f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{_WORKER_ROLE}') THEN "
        f"CREATE ROLE {_WORKER_ROLE} LOGIN PASSWORD '{_ROLE_PASSWORD}' BYPASSRLS; "
        "END IF; END $$"
    )
    # The worker role must pre-exist: creating a BYPASSRLS role needs privileges the app
    # role (rightly) lacks; migration 0003 then skips creation and only grants.
    await _admin_exec(
        admin_url,
        role_ddl,
        f"ALTER ROLE {_WORKER_ROLE} PASSWORD '{_ROLE_PASSWORD}'",
        f'CREATE DATABASE "{name}" OWNER {_APP_ROLE}',
    )
    app_url = _with_credentials(admin_url, _APP_ROLE, _ROLE_PASSWORD, name)
    worker_url = _with_credentials(admin_url, _WORKER_ROLE, _ROLE_PASSWORD, name)
    try:
        await asyncio.to_thread(_migrate, app_url)  # env.py runs its own event loop
        app_engine, worker_engine = create_async_engine(app_url), create_async_engine(worker_url)
        yield Databases(
            app=async_sessionmaker(app_engine, expire_on_commit=False),
            worker=async_sessionmaker(worker_engine, expire_on_commit=False),
        )
        await app_engine.dispose()
        await worker_engine.dispose()
    finally:
        await _admin_exec(admin_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest_asyncio.fixture
async def pg() -> AsyncIterator[Databases]:
    if _EXTERNAL_DB_URL:
        async for databases in _provision(_EXTERNAL_DB_URL):
            yield databases
    else:
        with _testcontainers_url() as admin_url:
            async for databases in _provision(admin_url):
                yield databases


@pytest.fixture
def pg_session_factory(pg: Databases) -> async_sessionmaker[AsyncSession]:
    """App-role sessions: subject to (forced) RLS, like the API and the job runner."""
    return pg.app


@pytest.fixture
def pg_worker_factory(pg: Databases) -> async_sessionmaker[AsyncSession]:
    """Worker-role sessions: BYPASSRLS, like the queue and the retention sweep."""
    return pg.worker


@pytest.mark.asyncio
async def test_rls_blocks_cross_tenant_manuscript_reads(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    uow = UnitOfWork(pg_session_factory)

    async with pg_worker_factory() as setup_session:  # tenants: no RLS
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

    # The app role OWNS these tables (it ran the migrations, as in .env.example).
    # Without migration 0004's FORCE ROW LEVEL SECURITY the owner bypasses every
    # policy, so a query that forgets both the WHERE clause and the tenant context
    # would see all tenants. With it, the owner sees nothing without a tenant context.
    async with pg_session_factory() as unscoped:
        count = (await unscoped.execute(text("SELECT count(*) FROM manuscripts"))).scalar_one()
        assert count == 0
    async with pg_worker_factory() as worker:  # BYPASSRLS role still sees everything
        count = (await worker.execute(text("SELECT count(*) FROM manuscripts"))).scalar_one()
        assert count == 1


@pytest.mark.asyncio
async def test_skip_locked_claim_never_double_claims_under_concurrency(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_worker_factory() as session:
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
        async with pg_worker_factory() as session:
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
        # What the upload does: the credit is taken before the job is queued.
        await CreditLedgerRepository(session).append_once(
            tenant_id=tenant_id,
            delta=-1,
            reason="job",
            idempotency_key=f"job:{job.id}:reserve",
            job_id=job.id,
        )
        await session.commit()
    return tenant_id, manuscript.id, job.id


@pytest.mark.asyncio
async def test_ledger_append_once_is_idempotent_inside_one_transaction(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_worker_factory() as session:
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
        assert await ledger.entry_delta("stripe:evt_1") is not None

    async with uow.begin(tenant_id) as session:
        assert await CreditLedgerRepository(session).balance(tenant_id) == 10


@pytest.mark.asyncio
async def test_operator_can_grant_credits_without_stripe(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_worker_factory() as session:
        tenant_id = (await TenantRepository(session).create(name="Pilot")).id
        await session.commit()
    uow = UnitOfWork(pg_session_factory)

    assert await grant_credits(uow, tenant_id, 5) == 5
    assert await grant_credits(uow, tenant_id, 3) == 8  # every grant is its own entry
    assert await grant_credits(uow, tenant_id, -10) == -2  # a refunded purchase taken back
    with pytest.raises(EdittaError, match="not be 0"):
        await grant_credits(uow, tenant_id, 0)
    with pytest.raises(EdittaError, match="not found"):
        await grant_credits(uow, uuid4(), 5)


@pytest.mark.asyncio
async def test_one_tenants_batch_does_not_hold_back_another_tenant(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def _queue(tenant_id: UUID) -> UUID:
        async with pg_worker_factory() as session:  # one transaction each: distinct now()
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
            return job.id

    async with pg_worker_factory() as session:
        big = (await TenantRepository(session).create(name="Big")).id
        small = (await TenantRepository(session).create(name="Small")).id
        await session.commit()
    batch = [await _queue(big) for _ in range(3)]
    late = await _queue(small)  # uploaded after the whole batch

    async with pg_worker_factory() as session:
        queue = PostgresJobQueue(session)
        order = [(await queue.claim(worker_id=f"w{i}")) for i in range(4)]

    assert [c.job_id for c in order if c is not None] == [batch[0], late, batch[1], batch[2]]


@pytest.mark.asyncio
async def test_deferred_job_is_not_claimable_until_its_backoff_elapses(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, _, job_id = await _seed_job(pg_worker_factory)

    async with pg_worker_factory() as session:
        queue = PostgresJobQueue(session)
        first = await queue.claim(worker_id="w1")
        assert first is not None and first.attempt == 1  # the claim counts the attempt
        await queue.defer(job_id, "w1", delay_seconds=3600)
        assert await queue.claim(worker_id="w2") is None  # backing off

        async with pg_worker_factory() as admin:  # fast-forward the backoff
            await admin.execute(
                text("UPDATE analysis_jobs SET lease_expires_at = now() - interval '1 s'")
            )
            await admin.commit()
        reclaimed = await queue.claim(worker_id="w2")
        assert reclaimed is not None and reclaimed.job_id == job_id
        assert (reclaimed.attempt, reclaimed.lease_owner) == (2, "w2")


@pytest.mark.asyncio
async def test_dead_job_is_never_claimed_again(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _, job_id = await _seed_job(pg_worker_factory)
    async with pg_worker_factory() as session:
        claimed = await PostgresJobQueue(session).claim(worker_id="w")
        assert claimed is not None

    async with UnitOfWork(pg_session_factory).begin(tenant_id) as session:
        await AnalysisJobRepository(session).set_state_if_owned(
            tenant_id, job_id, lease_owner="w", state=JobState.DEAD, error="boom"
        )
    async with pg_worker_factory() as session:
        queue = PostgresJobQueue(session)
        await queue.release(job_id, "w")
        assert await queue.claim(worker_id="w") is None


@pytest.mark.asyncio
async def test_queue_health_counts_dead_jobs_and_the_oldest_claimable_wait(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_worker_factory() as session:
        empty = await PostgresJobQueue(session).health(dead_window_seconds=3600)
    assert (empty.dead_recently, empty.oldest_waiting_seconds) == (0, None)

    tenant_id, _, waiting_id = await _seed_job(pg_worker_factory)
    _, _, dead_id = await _seed_job(pg_worker_factory)
    _, _, customer_dead_id = await _seed_job(pg_worker_factory)
    async with pg_worker_factory() as session:
        # A scanned PDF: refunded, the customer's input, not worth an alert.
        await session.execute(
            text(
                "UPDATE analysis_jobs SET state = 'dead', failure_kind = 'customer' WHERE id = :i"
            ),
            {"i": customer_dead_id},
        )
        await session.execute(
            text("UPDATE analysis_jobs SET created_at = now() - interval '2 hours' WHERE id = :i"),
            {"i": waiting_id},
        )
        await session.execute(
            text("UPDATE analysis_jobs SET state = 'dead' WHERE id = :i"), {"i": dead_id}
        )
        await session.commit()

    async with pg_worker_factory() as session:
        queue = PostgresJobQueue(session)
        health = await queue.health(dead_window_seconds=3600)
        assert health.dead_recently == 1
        assert health.oldest_waiting_seconds is not None
        assert 7000 < health.oldest_waiting_seconds < 7400  # the dead job is not "waiting"

        # Claimed (lease held) -> nobody is waiting any more.
        assert await queue.claim(worker_id="w") is not None
        held = await queue.health(dead_window_seconds=3600)
        assert held.oldest_waiting_seconds is None

        # A dead job older than the window no longer counts.
        await session.execute(
            text("UPDATE analysis_jobs SET updated_at = now() - interval '2 hours' WHERE id = :i"),
            {"i": dead_id},
        )
        await session.commit()
        assert (await queue.health(dead_window_seconds=3600)).dead_recently == 0


@pytest.mark.asyncio
async def test_a_worker_that_lost_its_lease_cannot_write_release_or_defer(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _, job_id = await _seed_job(pg_worker_factory)
    async with pg_worker_factory() as session:
        queue = PostgresJobQueue(session)
        assert await queue.claim(worker_id="w1", lease_seconds=0) is not None
        await asyncio.sleep(0.01)  # w1's lease lapses (it stalled)
        assert await queue.claim(worker_id="w2") is not None

        await queue.release(job_id, "w1")  # fenced: must not free w2's job
        await queue.defer(job_id, "w1", delay_seconds=0)
        assert await queue.claim(worker_id="w3") is None

    with pytest.raises(LeaseLost):
        async with UnitOfWork(pg_session_factory).begin(tenant_id) as session:
            await AnalysisJobRepository(session).set_state_if_owned(
                tenant_id, job_id, lease_owner="w1", state=JobState.DEAD, error="stale"
            )
    async with UnitOfWork(pg_session_factory).begin(tenant_id) as session:
        job = await AnalysisJobRepository(session).get_by_id(tenant_id, job_id)
        assert job is not None and job.state == JobState.PENDING and job.lease_owner == "w2"


@pytest.mark.asyncio
async def test_report_create_is_idempotent_per_job_and_kind(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _, job_id = await _seed_job(pg_worker_factory)
    uow = UnitOfWork(pg_session_factory)

    for key in ("jobs/a/report.docx", "jobs/b/report.docx"):
        async with uow.begin(tenant_id) as session:
            await ReportRepository(session).create(
                job_id=job_id, kind=ArtifactKind.REPORT, blob_key=key
            )

    async with uow.begin(tenant_id) as session:
        report = await ReportRepository(session).get(job_id, ArtifactKind.REPORT)
        assert report is not None and report.blob_key == "jobs/b/report.docx"


@pytest.mark.asyncio
async def test_a_job_may_have_one_row_per_kind_but_not_two_of_a_kind(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Migration 0006: UNIQUE(job_id) became UNIQUE(job_id, kind). Kinds beyond REPORT
    arrive with later features, so the row is inserted directly."""
    tenant_id, _, job_id = await _seed_job(pg_worker_factory)
    uow = UnitOfWork(pg_session_factory)

    async with uow.begin(tenant_id) as session:
        await ReportRepository(session).create(
            job_id=job_id, kind=ArtifactKind.REPORT, blob_key="jobs/x/report.docx"
        )
        await session.execute(
            pg_insert(Report).values(
                id=uuid4(), job_id=job_id, kind="annotated_manuscript", blob_key="jobs/x/a.docx"
            )
        )
    async with uow.begin(tenant_id) as session:
        kinds = (
            await session.execute(select(Report.kind).where(Report.job_id == job_id))
        ).scalars()
        assert sorted(kinds) == ["annotated_manuscript", "report"]


@pytest.mark.asyncio
async def test_migration_0006_round_trips_and_keeps_existing_reports_as_kind_report(
    pg: Databases,
) -> None:
    app_url = pg.app.kw["bind"].url.render_as_string(hide_password=False)

    await asyncio.to_thread(_migrate, app_url, revision="0005", down=True)
    await asyncio.to_thread(_migrate, app_url)

    async with pg.worker() as session:
        columns = await session.execute(
            text(
                "select column_default from information_schema.columns "
                "where table_name = 'reports' and column_name = 'kind'"
            )
        )
        assert "report" in columns.scalar_one()


@pytest.mark.asyncio
async def test_failed_attempt_keeps_checkpoints_and_retry_completes_on_real_db(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """End-to-end regression for the single-transaction bug, on real Postgres: a pass
    failure must leave completed checkpoints, the attempt count and the retry state
    committed, and the retry must finish the job without re-running those passes."""
    blobs = LocalBlobStore(tmp_path)
    await blobs.put("raw/essay", _chunk(_docx_bytes(MANUSCRIPT_TEXT)))
    tenant_id, manuscript_id, job_id = await _seed_job(
        pg_worker_factory, blob_key="raw/essay", title="essay.docx"
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

    async with pg_worker_factory() as session:
        claimed = await PostgresJobQueue(session).claim(worker_id="w1")
    assert claimed is not None and claimed.job_id == job_id
    first = await runner.run(claimed)

    assert first.kind is JobOutcomeKind.RETRY
    async with uow.begin(tenant_id) as session:
        job = await AnalysisJobRepository(session).get_by_id(tenant_id, job_id)
        rows = await PassResultRepository(session).list_for_job(job_id)
        ledger = CreditLedgerRepository(session)
        assert job is not None and (job.state, job.attempt) == (JobState.PENDING, 1)
        assert {r.pass_name for r in rows} == {"language", "metadata", "integrity"}
        assert await ledger.entry_delta(f"job:{job_id}:reserve") is not None
        assert await ledger.entry_delta(f"job:{job_id}:refund") is None

    async with pg_worker_factory() as session:
        queue = PostgresJobQueue(session)
        await queue.defer(job_id, "w1", delay_seconds=0)
        await asyncio.sleep(0.01)
        retry = await queue.claim(worker_id="w1")
    assert retry is not None and retry.attempt == 2

    failing["active"] = False
    calls_before = len(llm.calls)
    second = await runner.run(retry)

    assert second.kind is JobOutcomeKind.COMPLETED
    assert len(llm.calls) - calls_before == 2  # structure + synthesis
    async with uow.begin(tenant_id) as session:
        job = await AnalysisJobRepository(session).get_by_id(tenant_id, job_id)
        assert job is not None and job.state == JobState.COMPLETED
        assert await ReportRepository(session).get(job_id, ArtifactKind.REPORT) is not None
        assert await CreditLedgerRepository(session).balance(tenant_id) == -1


@pytest.mark.asyncio
async def test_stale_worker_cannot_kill_or_refund_a_job_another_worker_completed(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The reviewer's free-report scenario, on real Postgres: w1 stalls past its lease,
    w2 reclaims and completes the job, then w1 fails deterministically. Before fencing,
    w1 would overwrite COMPLETED with DEAD and refund the credit."""
    blobs = LocalBlobStore(tmp_path)
    await blobs.put("raw/essay", _chunk(_docx_bytes(MANUSCRIPT_TEXT)))
    tenant_id, _, job_id = await _seed_job(
        pg_worker_factory, blob_key="raw/essay", title="essay.docx"
    )
    uow = UnitOfWork(pg_session_factory)
    async with pg_worker_factory() as session:
        queue = PostgresJobQueue(session)
        stale = await queue.claim(worker_id="w1", lease_seconds=0)
        await asyncio.sleep(0.01)
        fresh = await queue.claim(worker_id="w2")
    assert stale is not None and fresh is not None

    ok_llm = FakeLLMClient(manuscript_text=MANUSCRIPT_TEXT)
    done = await JobRunner(
        uow=job_unit_of_work(uow, tenant_id), blobs=blobs, llm=ok_llm, cost_ceiling_usd=6.0
    ).run(fresh)
    doomed_llm = FakeLLMClient(
        manuscript_text=MANUSCRIPT_TEXT, fail_when=lambda _parts: ParseError("boom")
    )
    late = await JobRunner(
        uow=job_unit_of_work(uow, tenant_id), blobs=blobs, llm=doomed_llm, cost_ceiling_usd=6.0
    ).run(stale)

    assert done.kind is JobOutcomeKind.COMPLETED
    assert late.kind is JobOutcomeKind.ABANDONED
    async with uow.begin(tenant_id) as session:
        job = await AnalysisJobRepository(session).get_by_id(tenant_id, job_id)
        assert job is not None and job.state == JobState.COMPLETED
        assert await CreditLedgerRepository(session).entry_delta(f"job:{job_id}:refund") is None


MANUSCRIPT_TEXT = "This is a Greek essay about language and structure and much more content here."


def _docx_bytes(text: str) -> bytes:
    document = docx.Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


async def _chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _accounts(
    factory: async_sessionmaker[AsyncSession],
) -> AccountService:
    """Wired exactly like api/deps.py and the CLI: user lookups in tenant-scoped
    transactions on the RLS-forced app role."""
    uow = UnitOfWork(factory)

    @asynccontextmanager
    async def users(tenant_id: UUID) -> AsyncIterator[UserRepository]:
        async with uow.begin(tenant_id) as session:
            yield UserRepository(session)

    @asynccontextmanager
    async def tenants() -> AsyncIterator[TenantRepository]:
        async with factory() as session, session.begin():
            yield TenantRepository(session)

    return AccountService(users=users, tenants=tenants, session_secret="integration-secret")  # noqa: S106


@pytest.mark.asyncio
async def test_invite_only_login_works_under_forced_rls(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    accounts = _accounts(pg_session_factory)
    admin = await accounts.bootstrap_tenant(name="Εκδόσεις", admin_email="Owner@Polis.gr")

    stranger = await accounts.request_login(
        tenant_id=admin.tenant_id, email="stranger@example.org", base_url="x"
    )
    login = await accounts.request_login(
        tenant_id=admin.tenant_id, email="owner@polis.gr", base_url="x"
    )
    assert stranger is None and login is not None
    user = await accounts.complete_login(login.url.split("token=", 1)[1])
    cookie = accounts.issue_session_cookie(
        tenant_id=user.tenant_id, user_id=user.user_id, email=user.email
    )

    assert (await accounts.authenticate_session(cookie)).role == "admin"
    async with UnitOfWork(pg_session_factory).begin(admin.tenant_id) as session:
        users = UserRepository(session)
        assert await users.get_by_email(admin.tenant_id, "stranger@example.org") is None


@pytest.mark.asyncio
async def test_concurrent_uploads_cannot_both_spend_the_last_credit(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        tenant_id = (await TenantRepository(session).create(name="Tenant")).id
        await session.commit()
    uow = UnitOfWork(pg_session_factory)
    async with uow.begin(tenant_id) as session:
        await CreditLedgerRepository(session).append_once(
            tenant_id=tenant_id, delta=1, reason="purchase", idempotency_key="stripe:evt"
        )
    blobs = FakeBlobStore()

    async def upload() -> str:
        async with uow.begin(tenant_id) as session:
            try:
                await submit_manuscript(
                    tenant_id=tenant_id,
                    actor="editor@example.com",
                    data=_docx_bytes(MANUSCRIPT_TEXT),
                    filename="essay.docx",
                    manuscripts=ManuscriptRepository(session),
                    jobs=AnalysisJobRepository(session),
                    audit=AuditEventRepository(session),
                    tenants=TenantRepository(session),
                    ledger=CreditLedgerRepository(session),
                    blobs=blobs,
                    retention_days=30,
                )
            except InsufficientCredits:
                return "refused"
        return "accepted"

    results = sorted(await asyncio.gather(upload(), upload()))

    assert results == ["accepted", "refused"]
    async with uow.begin(tenant_id) as session:
        assert await CreditLedgerRepository(session).balance(tenant_id) == 0


@pytest.mark.asyncio
async def test_queue_connection_must_bypass_rls(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Under forced RLS a worker on the app role would claim nothing, silently. The
    startup check makes that misconfiguration an error instead."""
    app_engine = pg_session_factory.kw["bind"]
    worker_engine = pg_worker_factory.kw["bind"]

    with pytest.raises(MisconfiguredRole, match="EDITTA_WORKER_DATABASE_URL"):
        await require_rls_bypass(app_engine, purpose="the job queue")
    await require_rls_bypass(worker_engine, purpose="the job queue")

    async with pg_session_factory() as session:  # and this is what it would have done
        assert await PostgresJobQueue(session).claim(worker_id="w") is None


@pytest.mark.asyncio
async def test_retention_purges_an_analysed_manuscript_on_real_db(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Regression: the sweep deleted the manuscript row, which jobs reference, so it
    failed with a foreign-key violation on every analysed manuscript — after deleting
    the source file — and never removed the checkpoints that quote the text."""
    blobs = LocalBlobStore(tmp_path)
    tenant_id, manuscript_id, job_id = await _seed_job(
        pg_worker_factory, blob_key="t/manuscripts/m/source", title="essay.docx"
    )
    checkpoint = f"jobs/{job_id}/passes/language.json"
    extra = f"jobs/{job_id}/annotated_manuscript.docx"
    for key in ("t/manuscripts/m/source", checkpoint, f"jobs/{job_id}/report.docx", extra):
        await blobs.put(key, _chunk(b"quoted manuscript text"))
    uow = UnitOfWork(pg_session_factory)
    async with uow.begin(tenant_id) as session:
        await PassResultRepository(session).upsert(
            job_id=job_id,
            pass_name="language",  # noqa: S106
            blob_key=checkpoint,
            model="m",
            usage={},
        )
        await ReportRepository(session).create(
            job_id=job_id, kind=ArtifactKind.REPORT, blob_key=f"jobs/{job_id}/report.docx"
        )
        # A further artifact kind (F1's annotated manuscript): same purge, no new code.
        await session.execute(
            pg_insert(Report).values(
                id=uuid4(),
                job_id=job_id,
                kind="annotated_manuscript",
                blob_key=f"jobs/{job_id}/annotated_manuscript.docx",
            )
        )

    result = await purge_expired(
        retention_unit_of_work(pg_worker_factory), blobs, now=datetime.now(UTC) + timedelta(days=1)
    )

    assert (result.purged, result.failed) == (1, 0)
    assert not (tmp_path / "t/manuscripts/m/source").exists()
    assert not (tmp_path / checkpoint).exists()
    # The report quotes the manuscript too, so retention removes it, file and row.
    assert not (tmp_path / f"jobs/{job_id}/report.docx").exists()
    assert not (tmp_path / extra).exists()
    async with uow.begin(tenant_id) as session:
        manuscript = await ManuscriptRepository(session).get_by_id(tenant_id, manuscript_id)
        assert manuscript is not None and manuscript.status == "purged"
        assert await PassResultRepository(session).list_for_job(job_id) == []
        assert await ReportRepository(session).get(job_id, ArtifactKind.REPORT) is None
        remaining = await session.execute(select(Report.id).where(Report.job_id == job_id))
        assert remaining.first() is None


@pytest.mark.asyncio
async def test_concurrent_invites_of_one_email_both_succeed_with_the_same_user(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression (hunt D3): the loser of the race raised IntegrityError (an HTTP 500)
    although the user existed — 100 failures in 100 trials before the fix."""
    async with pg_worker_factory() as session:
        tenant_id = (await TenantRepository(session).create(name="Tenant")).id
        await session.commit()
    uow = UnitOfWork(pg_session_factory)

    @asynccontextmanager
    async def users(tid: UUID) -> AsyncIterator[UserRepository]:
        async with uow.begin(tid) as session:
            yield UserRepository(session)

    accounts = AccountService(users=users, session_secret="secret")  # noqa: S106

    for i in range(20):
        email = f"person{i}@example.com"
        first, second = await asyncio.gather(
            accounts.provision_user(tenant_id=tenant_id, email=email),
            accounts.provision_user(tenant_id=tenant_id, email=email),
        )
        assert first.user_id == second.user_id


@pytest.mark.asyncio
async def test_inviting_different_emails_still_creates_separate_users(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_worker_factory() as session:
        tenant_id = (await TenantRepository(session).create(name="Tenant")).id
        await session.commit()
    uow = UnitOfWork(pg_session_factory)

    async with uow.begin(tenant_id) as session:
        repo = UserRepository(session)
        a = await repo.create(tenant_id=tenant_id, email="a@example.com", role="admin")
        b = await repo.create(tenant_id=tenant_id, email="b@example.com")
        assert a.id != b.id and (a.role, b.role) == ("admin", "editor")


@pytest.mark.asyncio
async def test_token_usage_recorded_concurrently_adds_up(
    pg_session_factory: async_sessionmaker[AsyncSession],
    pg_worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Passes on different models checkpoint at the same time, each in its own
    transaction. A read-add-write lost updates; the counters must be exact."""
    tenant_id, _, job_id = await _seed_job(pg_worker_factory)
    uow = UnitOfWork(pg_session_factory)

    async def _record() -> None:
        async with uow.begin(tenant_id) as session:
            await AnalysisJobRepository(session).record_usage(
                tenant_id, job_id, input_tokens=1, output_tokens=2, cache_read_tokens=3
            )

    await asyncio.gather(*(_record() for _ in range(20)))

    async with uow.begin(tenant_id) as session:
        job = await AnalysisJobRepository(session).get_by_id(tenant_id, job_id)
    assert job is not None
    assert (job.cost_input_tokens, job.cost_output_tokens, job.cache_read_tokens) == (20, 40, 60)
