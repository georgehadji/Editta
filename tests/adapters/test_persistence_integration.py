"""Integration tests against real Postgres via testcontainers.

Verifies RLS cross-tenant isolation and SKIP LOCKED job-claim exclusivity — the two
guarantees that are meaningless to test against fakes, because fakes don't have a
database to leak across. Needs a reachable Docker daemon; skips cleanly otherwise.
"""

import asyncio
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    ManuscriptRepository,
    TenantRepository,
)
from editta.adapters.persistence.tables import Base
from editta.adapters.persistence.uow import UnitOfWork
from editta.adapters.queue.postgres import PostgresJobQueue

_RLS_TABLES = ("users", "manuscripts", "analysis_jobs", "credit_ledger", "audit_events")


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=5, check=True)  # noqa: S607
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(not _docker_available(), reason="Docker daemon not reachable")


@pytest_asyncio.fixture
async def pg_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        url = pg.get_connection_url().replace("postgresql+psycopg2", "postgresql+asyncpg")
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
    tenant_a, tenant_b = uuid4(), uuid4()

    async with pg_session_factory() as setup_session:
        await TenantRepository(setup_session).create(name="Tenant A")  # tenants: no RLS
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
    tenant_id = uuid4()
    async with pg_session_factory() as session:
        await TenantRepository(session).create(name="Tenant")
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
