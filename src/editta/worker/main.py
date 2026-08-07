"""Worker entrypoint: claim -> run pipeline -> graceful SIGTERM drain.

Claim/heartbeat/release use a plain (non tenant-scoped) session against
WORKER_DATABASE_URL — see infra/migrations 0002-0003 for why that connection needs
a BYPASSRLS role. Running the job itself uses UnitOfWork.begin(tenant_id), so the
actual pipeline I/O stays RLS-scoped like any other tenant-scoped code path.
"""

import asyncio
import signal
import socket
import uuid
from contextlib import suppress

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from editta.adapters.llm.client import AnthropicLLMClient
from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    CreditLedgerRepository,
    ManuscriptRepository,
    PassResultRepository,
    ReportRepository,
)
from editta.adapters.persistence.uow import UnitOfWork
from editta.adapters.queue.postgres import DEFAULT_LEASE_SECONDS, PostgresJobQueue
from editta.adapters.storage.s3 import S3BlobStore
from editta.config import Settings, load_settings, worker_database_url
from editta.domain.errors import EdittaError
from editta.domain.models import ClaimedJob
from editta.services.jobs import JobRunner

logger = structlog.get_logger(__name__)

HEARTBEAT_INTERVAL_SECONDS = DEFAULT_LEASE_SECONDS // 3
POLL_INTERVAL_SECONDS = 2.0


class Worker:
    def __init__(self, settings: Settings) -> None:
        self._worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self._queue_engine = create_async_engine(worker_database_url(settings))
        self._queue_sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._queue_engine, expire_on_commit=False
        )
        app_engine = create_async_engine(settings.database_url)
        app_sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
            app_engine, expire_on_commit=False
        )
        self._uow = UnitOfWork(app_sessions)
        self._blobs = S3BlobStore(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
        )
        self._llm = AnthropicLLMClient(
            settings.anthropic_api_key, max_concurrency=settings.max_llm_concurrency
        )
        self._shutdown = asyncio.Event()

    def request_shutdown(self) -> None:
        logger.info("worker.shutdown_requested", worker_id=self._worker_id)
        self._shutdown.set()

    async def run_forever(self) -> None:
        logger.info("worker.started", worker_id=self._worker_id)
        while not self._shutdown.is_set():
            claimed = await self._claim_one()
            if claimed is None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._shutdown.wait(), timeout=POLL_INTERVAL_SECONDS)
                continue
            await self._process(claimed)
        logger.info("worker.drained", worker_id=self._worker_id)

    async def _claim_one(self) -> ClaimedJob | None:
        async with self._queue_sessions() as session:
            return await PostgresJobQueue(session).claim(worker_id=self._worker_id)

    async def _process(self, claimed: ClaimedJob) -> None:
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(claimed.job_id))
        try:
            async with self._uow.begin(claimed.tenant_id) as session:
                runner = JobRunner(
                    jobs=AnalysisJobRepository(session),
                    manuscripts=ManuscriptRepository(session),
                    pass_results=PassResultRepository(session),
                    reports=ReportRepository(session),
                    ledger=CreditLedgerRepository(session),
                    blobs=self._blobs,
                    llm=self._llm,
                )
                await runner.run(
                    tenant_id=claimed.tenant_id,
                    job_id=claimed.job_id,
                    manuscript_id=claimed.manuscript_id,
                )
            logger.info("worker.job_completed", job_id=str(claimed.job_id))
        except EdittaError as exc:
            logger.warning("worker.job_failed", job_id=str(claimed.job_id), error=str(exc))
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            async with self._queue_sessions() as session:
                await PostgresJobQueue(session).release(claimed.job_id)

    async def _heartbeat_loop(self, job_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            async with self._queue_sessions() as session:
                await PostgresJobQueue(session).heartbeat(job_id, self._worker_id)


async def _run(settings: Settings) -> None:
    worker = Worker(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.request_shutdown)
        except NotImplementedError:
            # Windows' proactor loop doesn't support add_signal_handler for SIGTERM.
            signal.signal(sig, lambda *_args: worker.request_shutdown())
    await worker.run_forever()


def main() -> None:
    structlog.configure(processors=[structlog.processors.JSONRenderer()])
    with suppress(ImportError):
        import uvloop

        uvloop.install()
    settings = load_settings()
    asyncio.run(_run(settings))


if __name__ == "__main__":
    main()
