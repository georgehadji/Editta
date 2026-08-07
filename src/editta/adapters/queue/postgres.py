"""JobQueue port + Postgres SKIP LOCKED implementation.

Claim doesn't touch job `state` — that stays the job runner's decision (it inspects
checkpointed PassResults to resume at the right pipeline stage). Claim only grants
lease ownership: any non-terminal job whose lease is free or expired is claimable,
which is how a crashed worker's job gets picked back up without any special-casing.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from editta.domain.models import ClaimedJob

DEFAULT_LEASE_SECONDS = 300

_CLAIM_SQL = text("""
    UPDATE analysis_jobs
    SET lease_owner = :worker_id, lease_expires_at = :lease_expires_at
    WHERE id = (
        SELECT id FROM analysis_jobs
        WHERE state NOT IN ('completed', 'failed', 'dead', 'cancelled')
          AND (lease_owner IS NULL OR lease_expires_at < :now)
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, tenant_id, manuscript_id, attempt
""")

_HEARTBEAT_SQL = text("""
    UPDATE analysis_jobs SET lease_expires_at = :expires_at
    WHERE id = :job_id AND lease_owner = :worker_id
""")

_RELEASE_SQL = text("""
    UPDATE analysis_jobs SET lease_owner = NULL, lease_expires_at = NULL WHERE id = :job_id
""")


class PostgresJobQueue:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self, *, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> ClaimedJob | None:
        now = datetime.now(UTC)
        result = await self._session.execute(
            _CLAIM_SQL,
            {
                "worker_id": worker_id,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "now": now,
            },
        )
        await self._session.commit()
        row = result.first()
        if row is None:
            return None
        return ClaimedJob(
            job_id=row.id,
            tenant_id=row.tenant_id,
            manuscript_id=row.manuscript_id,
            attempt=row.attempt,
        )

    async def heartbeat(
        self, job_id: UUID, worker_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> None:
        await self._session.execute(
            _HEARTBEAT_SQL,
            {
                "expires_at": datetime.now(UTC) + timedelta(seconds=lease_seconds),
                "job_id": job_id,
                "worker_id": worker_id,
            },
        )
        await self._session.commit()

    async def release(self, job_id: UUID) -> None:
        await self._session.execute(_RELEASE_SQL, {"job_id": job_id})
        await self._session.commit()
