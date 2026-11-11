"""What a job's state means to the person waiting on it. Pure: no I/O.

The job row encodes more than `state` says: a retrying job is PENDING with an `error`, a
dead job is refunded and `failure_kind` says whose fault it was, and a COMPLETED job whose
report rows retention deleted has nothing left to download. Every reader (dashboard,
JSON API, CLI) derives the user-facing phase here instead of re-deriving it with its own
ifs (docs/STATE_MACHINES_PLAN.md, item 3)."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from editta.domain.artifacts import ArtifactKind
from editta.domain.states import CUSTOMER_FAILURE, JobState


class JobPhase(StrEnum):
    QUEUED = "queued"  # pending, never failed
    RETRYING = "retrying"  # pending after a failed attempt, waiting out the backoff
    RUNNING = "running"  # parsing | analyzing | synthesizing | exporting
    READY = "ready"  # completed, report available
    EXPIRED = "expired"  # completed, files deleted by the retention policy
    FAILED_INPUT = "failed_input"  # dead because of the uploaded file: fix it, upload again
    FAILED_SYSTEM = "failed_system"  # dead on our side
    UNKNOWN = "unknown"  # a state this code does not know: shown, never raised on


class NextAction(StrEnum):
    WAIT = "wait"
    DOWNLOAD = "download"
    REUPLOAD = "reupload"
    CONTACT = "contact"


_LABELS = {
    JobPhase.QUEUED: "Queued",
    JobPhase.RETRYING: "Retrying",
    JobPhase.RUNNING: "Running",
    JobPhase.READY: "Ready",
    JobPhase.EXPIRED: "Expired",
    JobPhase.FAILED_INPUT: "Failed — credit refunded",
    JobPhase.FAILED_SYSTEM: "Failed — credit refunded",
    JobPhase.UNKNOWN: "Unknown",
}

# What the user can do about it, in plain words. None when there is nothing to say.
_HINTS = {
    JobPhase.RETRYING: "This attempt failed; Editta will try again automatically.",
    JobPhase.EXPIRED: "The report was deleted after the retention period. Upload the file again "
    "for a new one.",
    JobPhase.FAILED_INPUT: "Fix the file and upload it again.",
    JobPhase.FAILED_SYSTEM: "Something went wrong on our side. Upload again later, or contact us.",
}

_NEXT = {
    JobPhase.QUEUED: NextAction.WAIT,
    JobPhase.RETRYING: NextAction.WAIT,
    JobPhase.RUNNING: NextAction.WAIT,
    JobPhase.READY: NextAction.DOWNLOAD,
    JobPhase.EXPIRED: NextAction.REUPLOAD,
    JobPhase.FAILED_INPUT: NextAction.REUPLOAD,
    JobPhase.FAILED_SYSTEM: NextAction.CONTACT,
    JobPhase.UNKNOWN: NextAction.CONTACT,
}


@dataclass(frozen=True, slots=True)
class JobView:
    phase: JobPhase
    step: JobState | None  # the pipeline step while RUNNING, else None
    terminal: bool
    # A dead job's reservation is refunded in the same transaction (services/jobs.py).
    refunded: bool
    # Error text is user-safe by construction (domain/errors.py), so it is shown as is.
    message: str | None
    next_action: NextAction
    artifacts: tuple[ArtifactKind, ...]

    @property
    def downloadable(self) -> bool:
        return self.phase is JobPhase.READY

    @property
    def hint(self) -> str | None:
        return _HINTS.get(self.phase)

    @property
    def label(self) -> str:
        if self.phase is JobPhase.RUNNING and self.step is not None:
            return f"Running: {self.step.value}"
        return _LABELS[self.phase]


def job_view(
    state: str,
    error: str | None,
    failure_kind: str | None,
    artifacts: Collection[str],
) -> JobView:
    kinds = tuple(k for k in ArtifactKind if k.value in artifacts)
    try:
        job_state = JobState(state)
    except ValueError:
        return _view(JobPhase.UNKNOWN, None, terminal=False, message=error, artifacts=kinds)

    if job_state is JobState.PENDING:
        phase = JobPhase.RETRYING if error else JobPhase.QUEUED
        return _view(phase, None, terminal=False, message=error, artifacts=kinds)
    if job_state is JobState.COMPLETED:
        # Report rows and COMPLETED commit together, so a completed job without a report
        # is one whose files retention has deleted since.
        phase = JobPhase.READY if ArtifactKind.REPORT in kinds else JobPhase.EXPIRED
        return _view(phase, None, terminal=True, message=None, artifacts=kinds)
    if job_state is JobState.DEAD:
        customer = failure_kind == CUSTOMER_FAILURE
        phase = JobPhase.FAILED_INPUT if customer else JobPhase.FAILED_SYSTEM
        return _view(phase, None, terminal=True, message=error, artifacts=kinds)
    return _view(JobPhase.RUNNING, job_state, terminal=False, message=None, artifacts=kinds)


def _view(
    phase: JobPhase,
    step: JobState | None,
    *,
    terminal: bool,
    message: str | None,
    artifacts: tuple[ArtifactKind, ...],
) -> JobView:
    return JobView(
        phase=phase,
        step=step,
        terminal=terminal,
        refunded=phase in (JobPhase.FAILED_INPUT, JobPhase.FAILED_SYSTEM),
        message=message,
        next_action=_NEXT[phase],
        artifacts=artifacts,
    )


class LeaseStatus(StrEnum):
    """Who may claim the job right now (the rule in adapters/queue/postgres.py)."""

    FREE = "free"  # never claimed, or released: claimable
    HELD = "held"  # a worker owns it and heartbeats
    BACKING_OFF = "backing_off"  # parked by defer() until the lease time passes
    EXPIRED = "expired"  # the owner stopped heartbeating (crash, stall): claimable


def lease_status(
    lease_owner: str | None, lease_expires_at: datetime | None, now: datetime
) -> LeaseStatus:
    if lease_expires_at is None:
        return LeaseStatus.FREE
    if lease_expires_at < now:  # same comparison as the claim SQL
        return LeaseStatus.EXPIRED
    return LeaseStatus.HELD if lease_owner else LeaseStatus.BACKING_OFF
