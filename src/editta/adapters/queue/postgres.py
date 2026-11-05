"""JobQueue port + Postgres SKIP LOCKED implementation.

Claim doesn't touch job `state` — that stays the job runner's decision (it inspects
checkpointed PassResults to resume at the right pipeline stage). Claim only grants
lease ownership: any non-terminal job whose lease is free or expired is claimable,
which is how a crashed worker's job gets picked back up without any special-casing.

Claimability is decided by `lease_expires_at` alone:
  * NULL            -> free (released, or never claimed)
  * in the past     -> free (owner crashed or stopped heartbeating)
  * in the future   -> held: by `lease_owner` while a worker runs it, or by nobody
                       when `defer()` parked it for retry backoff.
That one rule gives crash recovery and retry backoff without a scheduler or a
second column.

Every claim increments `attempt`, so an attempt that ends without recording a failure
(worker crash, OOM, lost lease) still counts, and the job runner dead-letters a job
whose claimed attempt exceeds the limit instead of running it forever.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from editta.domain.models import ClaimedJob

DEFAULT_LEASE_SECONDS = 300

_CLAIM_SQL = text("""
    UPDATE analysis_jobs
    SET lease_owner = :worker_id, lease_expires_at = :lease_expires_at, attempt = attempt + 1
    WHERE id = (
        SELECT id FROM analysis_jobs
        WHERE state NOT IN ('completed', 'failed', 'dead', 'cancelled')
          AND (lease_expires_at IS NULL OR lease_expires_at < :now)
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

# Both fenced on lease_owner: a worker that lost its lease must not release or defer
# a job another worker now holds (that would free it for a third, concurrent claim).
_RELEASE_SQL = text("""
    UPDATE analysis_jobs SET lease_owner = NULL, lease_expires_at = NULL
    WHERE id = :job_id AND lease_owner = :worker_id
""")

_DEFER_SQL = text("""
    UPDATE analysis_jobs SET lease_owner = NULL, lease_expires_at = :not_before
    WHERE id = :job_id AND lease_owner = :worker_id
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
            lease_owner=worker_id,
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

    async def release(self, job_id: UUID, worker_id: str) -> None:
        await self._session.execute(_RELEASE_SQL, {"job_id": job_id, "worker_id": worker_id})
        await self._session.commit()

    async def defer(self, job_id: UUID, worker_id: str, *, delay_seconds: int) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be >= 0")
        not_before = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        await self._session.execute(
            _DEFER_SQL, {"job_id": job_id, "worker_id": worker_id, "not_before": not_before}
        )
        await self._session.commit()
