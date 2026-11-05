"""Tenant-scoped repositories. Every method takes tenant_id explicitly — defense in
depth alongside RLS: adapters/persistence/uow.py sets the RLS session context too,
belt and suspenders, not either/or.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from editta.adapters.persistence.tables import (
    AnalysisJob,
    AuditEvent,
    CreditLedger,
    Manuscript,
    PassResult,
    Report,
    Tenant,
    User,
)


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, name: str) -> Tenant:
        tenant = Tenant(id=uuid4(), name=name)
        self._session.add(tenant)
        await self._session.flush()
        return tenant

    async def get_by_id(self, tenant_id: UUID) -> Tenant | None:
        return await self._session.get(Tenant, tenant_id)


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, tenant_id: UUID, email: str, role: str = "editor") -> User:
        user = User(id=uuid4(), tenant_id=tenant_id, email=email, role=role)
        self._session.add(user)
        await self._session.flush()
        return user

    async def get_by_email(self, tenant_id: UUID, email: str) -> User | None:
        stmt = select(User).where(User.tenant_id == tenant_id, User.email == email)
        return (await self._session.execute(stmt)).scalar_one_or_none()


class ManuscriptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, tenant_id: UUID, title: str, blob_key: str, sha256: str, retention_until: datetime
    ) -> Manuscript:
        manuscript = Manuscript(
            id=uuid4(),
            tenant_id=tenant_id,
            title=title,
            blob_key=blob_key,
            sha256=sha256,
            retention_until=retention_until,
        )
        self._session.add(manuscript)
        await self._session.flush()
        return manuscript

    async def get_by_id(self, tenant_id: UUID, manuscript_id: UUID) -> Manuscript | None:
        stmt = select(Manuscript).where(
            Manuscript.tenant_id == tenant_id, Manuscript.id == manuscript_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_expired(self, before: datetime) -> list[Manuscript]:
        # Retention sweep is a platform-level job, not tenant-scoped by design.
        stmt = select(Manuscript).where(Manuscript.retention_until < before)
        return list((await self._session.execute(stmt)).scalars().all())

    async def delete(self, manuscript: Manuscript) -> None:
        await self._session.delete(manuscript)


class AnalysisJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, tenant_id: UUID, manuscript_id: UUID) -> AnalysisJob:
        job = AnalysisJob(id=uuid4(), tenant_id=tenant_id, manuscript_id=manuscript_id)
        self._session.add(job)
        await self._session.flush()
        return job

    async def get_by_id(self, tenant_id: UUID, job_id: UUID) -> AnalysisJob | None:
        stmt = select(AnalysisJob).where(
            AnalysisJob.tenant_id == tenant_id, AnalysisJob.id == job_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_tenant(self, tenant_id: UUID, *, limit: int = 50) -> list[AnalysisJob]:
        stmt = (
            select(AnalysisJob)
            .where(AnalysisJob.tenant_id == tenant_id)
            .order_by(AnalysisJob.created_at.desc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def update_state(
        self, tenant_id: UUID, job_id: UUID, *, state: str, error: str | None = None
    ) -> None:
        job = await self.get_by_id(tenant_id, job_id)
        if job is None:
            raise LookupError(f"job {job_id} not found for tenant {tenant_id}")
        job.state = state
        job.error = error
        await self._session.flush()

    async def record_failure(
        self, tenant_id: UUID, job_id: UUID, *, state: str, error: str, attempts_made: int
    ) -> None:
        """Persist the outcome of a failed attempt: the next state (pending to retry, or
        dead), the error, and the attempt count the retry policy decided on."""
        await self._session.execute(
            update(AnalysisJob)
            .where(AnalysisJob.tenant_id == tenant_id, AnalysisJob.id == job_id)
            .values(state=state, error=error, attempt=attempts_made)
        )

    async def record_usage(
        self,
        tenant_id: UUID,
        job_id: UUID,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
    ) -> None:
        job = await self.get_by_id(tenant_id, job_id)
        if job is None:
            raise LookupError(f"job {job_id} not found for tenant {tenant_id}")
        job.cost_input_tokens += input_tokens
        job.cost_output_tokens += output_tokens
        job.cache_read_tokens += cache_read_tokens
        await self._session.flush()


class PassResultRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self, *, job_id: UUID, pass_name: str, blob_key: str, model: str, usage: dict[str, Any]
    ) -> PassResult:
        stmt = select(PassResult).where(
            PassResult.job_id == job_id, PassResult.pass_name == pass_name
        )
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            existing.blob_key = blob_key
            existing.model = model
            existing.usage = usage
            await self._session.flush()
            return existing
        result = PassResult(
            id=uuid4(),
            job_id=job_id,
            pass_name=pass_name,
            blob_key=blob_key,
            model=model,
            usage=usage,
        )
        self._session.add(result)
        await self._session.flush()
        return result

    async def list_for_job(self, job_id: UUID) -> list[PassResult]:
        stmt = select(PassResult).where(PassResult.job_id == job_id)
        return list((await self._session.execute(stmt)).scalars().all())


class ReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, job_id: UUID, blob_key: str, summary_blob_key: str | None = None
    ) -> None:
        """Idempotent on job_id (UNIQUE): an export retried after a crash re-points the
        existing row at the freshly written blob instead of violating the constraint."""
        stmt = pg_insert(Report).values(
            id=uuid4(), job_id=job_id, blob_key=blob_key, summary_blob_key=summary_blob_key
        )
        await self._session.execute(
            stmt.on_conflict_do_update(
                index_elements=[Report.job_id],
                set_={"blob_key": stmt.excluded.blob_key},
            )
        )

    async def get_by_job_id(self, job_id: UUID) -> Report | None:
        stmt = select(Report).where(Report.job_id == job_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()


class CreditLedgerRepository:
    """Append-only. Balance is always derived, never stored/updated directly."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_once(
        self,
        *,
        tenant_id: UUID,
        delta: int,
        reason: str,
        idempotency_key: str,
        job_id: UUID | None = None,
        stripe_ref: str | None = None,
    ) -> bool:
        """Append unless an entry with this idempotency_key exists. Returns True if
        this call appended.

        INSERT ... ON CONFLICT DO NOTHING rather than catching IntegrityError: in
        Postgres a unique violation aborts the *whole* transaction, so a caught
        IntegrityError leaves the session unusable and its commit fails — which made
        webhook replays and job retries error out instead of being no-ops.
        """
        result = await self._session.execute(
            pg_insert(CreditLedger)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                delta=delta,
                reason=reason,
                job_id=job_id,
                stripe_ref=stripe_ref,
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(index_elements=[CreditLedger.idempotency_key])
            .returning(CreditLedger.id)
        )
        return result.scalar_one_or_none() is not None

    async def has_entry(self, idempotency_key: str) -> bool:
        stmt = select(CreditLedger.id).where(CreditLedger.idempotency_key == idempotency_key)
        return (await self._session.execute(stmt)).first() is not None

    async def balance(self, tenant_id: UUID) -> int:
        stmt = select(func.coalesce(func.sum(CreditLedger.delta), 0)).where(
            CreditLedger.tenant_id == tenant_id
        )
        return int((await self._session.execute(stmt)).scalar_one())


class AuditEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, *, tenant_id: UUID, actor: str, action: str, subject: str) -> AuditEvent:
        event = AuditEvent(
            id=uuid4(), tenant_id=tenant_id, actor=actor, action=action, subject=subject
        )
        self._session.add(event)
        await self._session.flush()
        return event
