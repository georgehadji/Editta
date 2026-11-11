import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from editta.domain.models import ClaimedJob, JobOutcome
from editta.worker import main as worker_main


@pytest.mark.asyncio
async def test_heartbeat_loop_survives_transient_database_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: one failed heartbeat used to kill the task, let the lease lapse, and
    re-raise from `await heartbeat_task` in _process — crashing the worker loop."""
    attempts = {"n": 0}

    @asynccontextmanager
    async def flaky_session() -> Any:
        attempts["n"] += 1
        raise ConnectionError("db blip")
        yield  # pragma: no cover

    monkeypatch.setattr(worker_main, "HEARTBEAT_INTERVAL_SECONDS", 0)
    worker = worker_main.Worker.__new__(worker_main.Worker)
    worker._worker_id = "w"
    worker._queue_sessions = flaky_session  # type: ignore[assignment]

    task = asyncio.create_task(worker._heartbeat_loop(uuid4()))
    while attempts["n"] < 3:
        await asyncio.sleep(0)
    assert not task.done()  # still beating after repeated failures

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_failed_claim_does_not_end_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: only the heartbeat survived a database blip; one failed claim in the
    2-second poll ended run_forever, and the process with it."""
    worker = worker_main.Worker.__new__(worker_main.Worker)
    worker._worker_id = "w"
    worker._queue_engine = None  # type: ignore[assignment]
    worker._shutdown = asyncio.Event()
    claims = {"n": 0}

    async def claim() -> None:
        claims["n"] += 1
        if claims["n"] == 1:
            raise ConnectionError("db restarting")
        worker._shutdown.set()

    monkeypatch.setattr(worker, "_claim_one", claim)
    monkeypatch.setattr(worker_main, "require_rls_bypass", AsyncMock())
    monkeypatch.setattr(worker_main, "POLL_INTERVAL_SECONDS", 0.01)

    await worker.run_forever()

    assert claims["n"] == 2  # polled again after the failure


def _worker_with(
    monkeypatch: pytest.MonkeyPatch, run: Any, queue_calls: list[tuple[Any, ...]]
) -> worker_main.Worker:
    """A Worker whose runner and queue are fakes: only the outcome -> queue-action mapping
    of `_process_claimed` is real."""

    class FakeRunner:
        def __init__(self, **_: object) -> None: ...

        async def run(self, job: object) -> object:
            return await run()

    class FakeQueue:
        def __init__(self, session: object) -> None: ...

        async def defer(self, job_id: object, worker_id: str, *, delay_seconds: int) -> None:
            queue_calls.append(("defer", worker_id, delay_seconds))

        async def release(self, job_id: object, worker_id: str) -> None:
            queue_calls.append(("release", worker_id))

    @asynccontextmanager
    async def session() -> Any:
        yield object()

    async def idle_heartbeat(job_id: object) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(worker_main, "JobRunner", FakeRunner)
    monkeypatch.setattr(worker_main, "PostgresJobQueue", FakeQueue)
    monkeypatch.setattr(worker_main, "job_unit_of_work", lambda uow, tenant_id: None)
    worker = worker_main.Worker.__new__(worker_main.Worker)
    worker._worker_id = "w"
    worker._queue_sessions = session  # type: ignore[assignment]
    worker._uow = None  # type: ignore[assignment]
    worker._blobs = None  # type: ignore[assignment]
    worker._llm = None  # type: ignore[assignment]
    worker._cost_ceiling_usd = 6.0
    worker._analysis_timeout = 1.0
    worker._synthesis_timeout = 1.0
    monkeypatch.setattr(worker, "_heartbeat_loop", idle_heartbeat)
    return worker


def _claimed() -> ClaimedJob:
    return ClaimedJob(
        job_id=uuid4(), tenant_id=uuid4(), manuscript_id=uuid4(), attempt=1, lease_owner="w"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (JobOutcome.completed(), [("release", "w")]),
        (JobOutcome.dead(error="boom"), [("release", "w")]),
        # The backoff is the point: a retry released at once would burn every attempt in seconds.
        (JobOutcome.retry(error="busy", after_seconds=120), [("defer", "w", 120)]),
        # Another worker owns the job now: touching the lease would free it for a third.
        (JobOutcome.abandoned(error="lease lost"), []),
    ],
)
async def test_each_outcome_maps_to_its_queue_action(
    monkeypatch: pytest.MonkeyPatch, outcome: JobOutcome, expected: list[tuple[Any, ...]]
) -> None:
    calls: list[tuple[Any, ...]] = []

    async def run() -> JobOutcome:
        return outcome

    worker = _worker_with(monkeypatch, run, calls)

    await worker._process_claimed(_claimed())

    assert calls == expected


@pytest.mark.asyncio
async def test_a_runner_that_could_not_record_its_failure_keeps_the_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Releasing a job whose failure was never recorded would re-run it with no trace;
    keeping the lease lets it expire and the claim count the attempt."""
    calls: list[tuple[Any, ...]] = []

    async def run() -> JobOutcome:
        raise ConnectionError("database is down")

    worker = _worker_with(monkeypatch, run, calls)

    await worker._process_claimed(_claimed())  # does not raise: the worker loop survives

    assert calls == []
