"""AnalysisJob state machine + retry policy (PLAN.md §3). Pure: no I/O, no clock."""

from dataclasses import dataclass
from enum import StrEnum


class JobState(StrEnum):
    PENDING = "pending"
    PARSING = "parsing"
    ANALYZING = "analyzing"
    SYNTHESIZING = "synthesizing"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD = "dead"
    CANCELLED = "cancelled"


_LINEAR = (
    JobState.PENDING,
    JobState.PARSING,
    JobState.ANALYZING,
    JobState.SYNTHESIZING,
    JobState.EXPORTING,
    JobState.COMPLETED,
)

_ALLOWED: dict[JobState, frozenset[JobState]] = {
    state: frozenset({_LINEAR[i + 1], JobState.FAILED, JobState.CANCELLED})
    for i, state in enumerate(_LINEAR[:-1])
}
_ALLOWED[JobState.COMPLETED] = frozenset()
_ALLOWED[JobState.FAILED] = frozenset({JobState.PENDING, JobState.DEAD})
_ALLOWED[JobState.DEAD] = frozenset()
_ALLOWED[JobState.CANCELLED] = frozenset()

TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.DEAD, JobState.CANCELLED})


def can_transition(current: JobState, target: JobState) -> bool:
    return target in _ALLOWED[current]


def transition(current: JobState, target: JobState) -> JobState:
    if not can_transition(current, target):
        raise ValueError(f"illegal job transition: {current} -> {target}")
    return target


# --- Retry policy ---------------------------------------------------------------

MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 60
RETRY_MAX_DELAY_SECONDS = 30 * 60


@dataclass(frozen=True, slots=True)
class FailureDecision:
    """What happens to a job after a failed attempt.

    `next_state` is reached from FAILED via the state machine (FAILED -> PENDING to
    retry, FAILED -> DEAD to give up). `retry_after_seconds` is only set on retry.
    """

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
    *, previous_attempts: int, retryable: bool, max_attempts: int = MAX_ATTEMPTS
) -> FailureDecision:
    attempts_made = previous_attempts + 1
    if retryable and attempts_made < max_attempts:
        next_state = transition(JobState.FAILED, JobState.PENDING)
        return FailureDecision(attempts_made, next_state, retry_delay_seconds(attempts_made))
    return FailureDecision(attempts_made, transition(JobState.FAILED, JobState.DEAD), None)
