"""Queue health check: run on a schedule, exits non-zero when someone should look.

Same idiom as worker/retention.py: run once, print one line, let the scheduler (cron, a
systemd timer, a monitoring wrapper) turn a non-zero exit into an alert. Two signals:
  * a job was dead-lettered in the last 24 h (it ran out of attempts or hit a permanent error
    of ours; a file the customer got wrong, such as a scanned PDF, does not count);
  * a claimable job has waited longer than an hour (no worker is taking it).
The queue is read across every tenant, so like the worker it needs the BYPASSRLS
`editta_worker` role.
"""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from editta.adapters.persistence.roles import require_rls_bypass
from editta.adapters.queue.postgres import PostgresJobQueue, QueueHealth
from editta.config import load_settings, worker_database_url
from editta.observability import configure_logging

DEAD_WINDOW_SECONDS = 24 * 3600.0
# A single busy worker can legitimately leave the next job waiting while it runs a long one
# (analysis timeout is 30 min), so the alarm sits well above that.
STUCK_AFTER_SECONDS = 3600.0


def problems(
    health: QueueHealth,
    *,
    dead_window_seconds: float = DEAD_WINDOW_SECONDS,
    stuck_after_seconds: float = STUCK_AFTER_SECONDS,
) -> list[str]:
    found: list[str] = []
    if health.dead_recently:
        found.append(
            f"{health.dead_recently} job(s) dead-lettered in the last "
            f"{dead_window_seconds / 3600:.0f}h"
        )
    waited = health.oldest_waiting_seconds
    if waited is not None and waited > stuck_after_seconds:
        found.append(f"oldest claimable job has waited {waited / 60:.0f} min")
    return found


async def _main_async() -> QueueHealth:
    settings = load_settings()
    engine = create_async_engine(worker_database_url(settings))
    await require_rls_bypass(engine, purpose="the queue health check")
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            return await PostgresJobQueue(session).health(dead_window_seconds=DEAD_WINDOW_SECONDS)
    finally:
        await engine.dispose()


def main() -> None:
    configure_logging("ops-check")
    health = asyncio.run(_main_async())
    found = problems(health)
    print("; ".join(found) if found else "queue ok")
    if found:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
