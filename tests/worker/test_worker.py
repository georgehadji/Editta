import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

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
