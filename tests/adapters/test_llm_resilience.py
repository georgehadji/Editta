import asyncio
import time

import pytest
from structlog.testing import capture_logs

from editta.adapters.llm.resilience import (
    COOLDOWN_SECONDS,
    BreakerState,
    CircuitBreaker,
    TokenBucket,
)
from editta.domain.errors import LLMError


def test_circuit_breaker_closed_by_default() -> None:
    breaker = CircuitBreaker()
    breaker.before_call()  # must not raise


def test_circuit_breaker_state_transitions() -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=60.0)
    assert breaker.state is BreakerState.CLOSED

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN

    breaker._opened_at = time.monotonic() - 61  # cooldown over
    assert breaker.state is BreakerState.HALF_OPEN

    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_circuit_breaker_failed_probe_reopens() -> None:
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60.0)
    breaker.record_failure()
    breaker._opened_at = time.monotonic() - 61
    breaker.before_call()  # the probe
    assert breaker.state is BreakerState.OPEN  # probe admission re-stamps the cooldown

    with capture_logs() as logs:
        breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert [entry["event"] for entry in logs] == ["llm.breaker_opened"]


def test_circuit_breaker_logs_each_transition_once() -> None:
    breaker = CircuitBreaker(failure_threshold=5, cooldown_seconds=60.0)
    with capture_logs() as logs:
        for _ in range(5):
            breaker.record_failure()
        breaker.record_failure()  # already open: no new transition logged
    opened = [entry for entry in logs if entry["event"] == "llm.breaker_opened"]
    assert len(opened) == 1
    assert opened[0]["consecutive_failures"] == 5

    breaker._opened_at = time.monotonic() - 61
    with capture_logs() as logs:
        breaker.before_call()  # probe admitted
        breaker.record_success()  # probe succeeded: closed
    assert [entry["event"] for entry in logs] == ["llm.breaker_probe", "llm.breaker_closed"]


def test_circuit_breaker_success_on_closed_logs_nothing() -> None:
    breaker = CircuitBreaker()
    with capture_logs() as logs:
        breaker.record_success()
    assert logs == []


def test_circuit_breaker_opens_after_threshold_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=3)
    for _ in range(2):
        breaker.record_failure()
    breaker.before_call()  # still closed, under threshold

    breaker.record_failure()
    with pytest.raises(LLMError, match="circuit breaker open"):
        breaker.before_call()


def test_circuit_breaker_resets_on_success() -> None:
    breaker = CircuitBreaker(failure_threshold=2)
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.before_call()  # only 1 consecutive failure after reset — still closed


def test_circuit_breaker_half_open_probe_allowed_after_cooldown() -> None:
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.0)
    breaker.record_failure()
    time.sleep(0.01)
    breaker.before_call()  # cooldown elapsed instantly (0.0s) — probe allowed through


def test_circuit_breaker_half_open_admits_one_probe_then_closes_on_success() -> None:
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60.0)
    breaker.record_failure()
    breaker._opened_at = time.monotonic() - 61  # cooldown over

    breaker.before_call()  # the probe
    with pytest.raises(LLMError, match="circuit breaker open"):
        breaker.before_call()  # a second caller waits for the probe's result

    breaker.record_success()
    breaker.before_call()
    breaker.before_call()


def test_circuit_breaker_lost_probe_is_replaced_after_another_cooldown() -> None:
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60.0)
    breaker.record_failure()
    breaker._opened_at = time.monotonic() - 61
    breaker.before_call()  # probe that never reports back (cancelled)

    breaker._opened_at = time.monotonic() - 61
    breaker.before_call()  # a new probe is allowed


def test_default_cooldown_matches_plan() -> None:
    assert COOLDOWN_SECONDS == 60.0


@pytest.mark.asyncio
async def test_token_bucket_allows_burst_up_to_capacity() -> None:
    bucket = TokenBucket(capacity=3, refill_per_second=1000)
    for _ in range(3):
        await asyncio.wait_for(bucket.acquire(), timeout=0.1)


@pytest.mark.asyncio
async def test_token_bucket_blocks_until_refill() -> None:
    bucket = TokenBucket(capacity=1, refill_per_second=100)
    await bucket.acquire()
    started = time.monotonic()
    await bucket.acquire()
    assert time.monotonic() - started >= 0.005
