"""AnalysisJob state machine: enum + allowed transitions (PLAN.md §3)."""

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


def can_transition(current: JobState, target: JobState) -> bool:
    return target in _ALLOWED[current]


def transition(current: JobState, target: JobState) -> JobState:
    if not can_transition(current, target):
        raise ValueError(f"illegal job transition: {current} -> {target}")
    return target
