"""Circuit breaker + token-bucket rate budget. Retries themselves are SDK-native (PLAN.md §4)."""

import asyncio
import time
from dataclasses import dataclass, field

from editta.domain.errors import LLMError

FAILURE_THRESHOLD = 5
COOLDOWN_SECONDS = 60.0


@dataclass
class CircuitBreaker:
    """Opens after `failure_threshold` consecutive failures; half-open probe after cooldown."""

    failure_threshold: int = FAILURE_THRESHOLD
    cooldown_seconds: float = COOLDOWN_SECONDS
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    def before_call(self) -> None:
        if self._opened_at is None:
            return
        if time.monotonic() - self._opened_at < self.cooldown_seconds:
            raise LLMError("circuit breaker open: LLM provider failing repeatedly")
        # Half-open: this call is the probe; everyone else waits another cooldown for its result.
        self._opened_at = time.monotonic()

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._opened_at = time.monotonic()


class TokenBucket:
    """Async token-bucket limiter aligned to the org's LLM rate limits."""

    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._refill_per_second = refill_per_second
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated_at
                self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_second)
                self._updated_at = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                await asyncio.sleep((cost - self._tokens) / self._refill_per_second)
