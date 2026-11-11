"""Postgres SKIP LOCKED job queue.

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

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from editta.domain.models import ClaimedJob
from editta.domain.states import CUSTOMER_FAILURE

DEFAULT_LEASE_SECONDS = 300

# Round-robin across tenants: a job's turn is its place in its own tenant's queue (how many
# older unfinished jobs that tenant has), oldest first within a turn. Plain created_at
# order made one tenant's batch of 30 uploads hold every other tenant back for hours.
_CLAIM_SQL = text("""
    UPDATE analysis_jobs
    SET lease_owner = :worker_id, lease_expires_at = :lease_expires_at, attempt = attempt + 1
    WHERE id = (
        SELECT id FROM analysis_jobs AS job
        WHERE state NOT IN ('completed', 'dead')
          AND (lease_expires_at IS NULL OR lease_expires_at < :now)
        ORDER BY (
            SELECT count(*) FROM analysis_jobs AS older
            WHERE older.tenant_id = job.tenant_id
              AND older.state NOT IN ('completed', 'dead')
              AND older.created_at < job.created_at
        ), created_at
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


# Counts across every tenant, so it needs the BYPASSRLS worker role (ops_check uses it).
# "Waiting" is the claim rule above: non-terminal and lease free or expired. A death caused by
# the customer's input (failure_kind 'customer') is not counted: it is refunded and needs
# nobody; unmarked deaths (NULL) are counted.
_HEALTH_SQL = text("""
    SELECT
        count(*) FILTER (
            WHERE state = 'dead'
              AND failure_kind IS DISTINCT FROM :customer
              AND updated_at >= now() - make_interval(secs => :window)
        ) AS dead_recently,
        extract(epoch FROM now() - min(created_at) FILTER (
            WHERE state NOT IN ('completed', 'dead')
              AND (lease_expires_at IS NULL OR lease_expires_at < now())
        )) AS oldest_waiting_seconds
    FROM analysis_jobs
""")


@dataclass(frozen=True)
class QueueHealth:
    dead_recently: int
    # None when nothing is waiting.
    oldest_waiting_seconds: float | None


class PostgresJobQueue:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def health(self, *, dead_window_seconds: float) -> QueueHealth:
        row = (
            await self._session.execute(
                _HEALTH_SQL, {"window": dead_window_seconds, "customer": CUSTOMER_FAILURE}
            )
        ).one()
        waiting = row.oldest_waiting_seconds
        return QueueHealth(
            dead_recently=row.dead_recently,
            oldest_waiting_seconds=None if waiting is None else float(waiting),
        )

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
