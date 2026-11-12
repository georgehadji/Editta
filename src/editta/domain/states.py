"""AnalysisJob states, legal transitions + retry policy (PLAN.md §3). Pure: no I/O, no clock.

The runner moves a job forward PENDING -> PARSING -> ANALYZING -> SYNTHESIZING ->
EXPORTING -> COMPLETED, and a resumed job starts again at PARSING. Every state write is
fenced twice in one SQL WHERE (repositories.py `set_state_if_owned`): on the lease, and
on `ALLOWED_FROM` below, so COMPLETED and DEAD can never be left."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class JobState(StrEnum):
    PENDING = "pending"
    PARSING = "parsing"
    ANALYZING = "analyzing"
    SYNTHESIZING = "synthesizing"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    DEAD = "dead"


# The queue's claim and health SQL bind this set (adapters/queue/postgres.py).
TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.DEAD})

_UNFINISHED = frozenset(JobState) - TERMINAL_STATES

# For each state, the states a job may enter it from. Nothing leaves COMPLETED or DEAD.
# A new job starts PENDING by the column default, not by a transition.
ALLOWED_FROM: Mapping[JobState, frozenset[JobState]] = {
    JobState.PENDING: _UNFINISHED,  # retry after a failed attempt
    JobState.PARSING: _UNFINISHED,  # first run, or a resume after a crash or retry
    JobState.ANALYZING: frozenset({JobState.PARSING}),
    JobState.SYNTHESIZING: frozenset({JobState.ANALYZING}),
    JobState.EXPORTING: frozenset({JobState.SYNTHESIZING}),
    JobState.COMPLETED: frozenset({JobState.EXPORTING}),
    JobState.DEAD: _UNFINISHED,  # failure, or attempts exhausted at claim
}

# `failure_kind` of a job that died because of what the customer uploaded (an unreadable
# file, a manuscript too long or too costly). It is refunded and needs no operator.
CUSTOMER_FAILURE = "customer"


# --- Retry policy ---------------------------------------------------------------

MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 60
RETRY_MAX_DELAY_SECONDS = 30 * 60


@dataclass(frozen=True, slots=True)
class FailureDecision:
    """What happens to a job after a failed attempt: back to PENDING to retry (with
    `retry_after_seconds`), or DEAD to give up."""

    attempts_made: int
    next_state: JobState
    retry_after_seconds: int | None

    @property
    def is_terminal(self) -> bool:
        return self.next_state is JobState.DEAD


def retry_delay_seconds(attempts_made: int) -> int:
    """Exponential backoff: 60s, 120s, 240s, ... capped at 30 min.

    Backoff (not immediate retry) matters because the dominant retryable failure is an
    LLM outage, during which the circuit breaker fails calls instantly — an immediate
    retry would burn all attempts in seconds and DEAD-letter jobs a short outage
    should only have delayed (PLAN.md §10: "reports delayed not failed").
    """
    if attempts_made < 1:
        raise ValueError("attempts_made must be >= 1")
    return min(RETRY_BASE_DELAY_SECONDS << (attempts_made - 1), RETRY_MAX_DELAY_SECONDS)


def decide_after_failure(
    *, attempts_made: int, retryable: bool, max_attempts: int = MAX_ATTEMPTS
) -> FailureDecision:
    """`attempts_made` includes the attempt that just failed (claims count attempts)."""
    if attempts_made < 1:
        raise ValueError("attempts_made must be >= 1")
    if retryable and attempts_made < max_attempts:
        return FailureDecision(attempts_made, JobState.PENDING, retry_delay_seconds(attempts_made))
    return FailureDecision(attempts_made, JobState.DEAD, None)


def attempts_exhausted(attempt: int, max_attempts: int = MAX_ATTEMPTS) -> bool:
    """True when a claim hands out an attempt beyond the limit — only possible when
    earlier attempts ended without recording a failure (worker crash, OOM, lost lease).
    The job is dead-lettered at claim time instead of running yet again."""
    return attempt > max_attempts
