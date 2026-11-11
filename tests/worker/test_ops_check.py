"""The queue-check verdict: pure, so it is tested without a database."""

import pytest

from editta.adapters.queue.postgres import QueueHealth
from editta.worker import ops_check
from editta.worker.ops_check import problems


def test_an_idle_healthy_queue_has_no_problems() -> None:
    assert problems(QueueHealth(dead_recently=0, oldest_waiting_seconds=None)) == []


def test_a_short_wait_is_normal() -> None:
    assert problems(QueueHealth(dead_recently=0, oldest_waiting_seconds=600)) == []


def test_a_dead_lettered_job_is_reported() -> None:
    found = problems(QueueHealth(dead_recently=2, oldest_waiting_seconds=None))

    assert found == ["2 job(s) dead-lettered in the last 24h"]


def test_a_job_nobody_takes_is_reported() -> None:
    found = problems(QueueHealth(dead_recently=0, oldest_waiting_seconds=7200))

    assert found == ["oldest claimable job has waited 120 min"]


def test_main_exits_non_zero_on_a_problem(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def sick() -> QueueHealth:
        return QueueHealth(dead_recently=1, oldest_waiting_seconds=None)

    monkeypatch.setattr(ops_check, "_main_async", sick)
    monkeypatch.setattr(ops_check, "configure_logging", lambda service: None)

    with pytest.raises(SystemExit) as stop:
        ops_check.main()

    assert stop.value.code == 1
    assert "dead-lettered" in capsys.readouterr().out
