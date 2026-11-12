from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from editta.cli import job_row
from editta.domain.job_view import (
    JobPhase,
    LeaseStatus,
    NextAction,
    job_view,
    lease_status,
)
from editta.domain.states import TERMINAL_STATES, JobState

REPORT = {"report"}


@pytest.mark.parametrize(
    ("state", "error", "failure_kind", "artifacts", "phase", "next_action"),
    [
        ("pending", None, None, set(), JobPhase.QUEUED, NextAction.WAIT),
        ("pending", "timed out", None, set(), JobPhase.RETRYING, NextAction.WAIT),
        ("parsing", None, None, set(), JobPhase.RUNNING, NextAction.WAIT),
        ("analyzing", None, None, set(), JobPhase.RUNNING, NextAction.WAIT),
        ("synthesizing", None, None, set(), JobPhase.RUNNING, NextAction.WAIT),
        ("exporting", None, None, set(), JobPhase.RUNNING, NextAction.WAIT),
        ("completed", None, None, REPORT, JobPhase.READY, NextAction.DOWNLOAD),
        ("completed", None, None, set(), JobPhase.EXPIRED, NextAction.REUPLOAD),
        ("dead", "bad file", "customer", set(), JobPhase.FAILED_INPUT, NextAction.REUPLOAD),
        ("dead", "gave up", None, set(), JobPhase.FAILED_SYSTEM, NextAction.CONTACT),
        ("awaiting_batch", None, None, set(), JobPhase.UNKNOWN, NextAction.CONTACT),
    ],
)
def test_every_row_shape_maps_to_one_phase(
    state: str,
    error: str | None,
    failure_kind: str | None,
    artifacts: set[str],
    phase: JobPhase,
    next_action: NextAction,
) -> None:
    view = job_view(state, error, failure_kind, artifacts)
    assert (view.phase, view.next_action) == (phase, next_action)


def test_terminal_matches_the_queue_terminal_states() -> None:
    for state in JobState:
        assert job_view(state.value, None, None, REPORT).terminal is (state in TERMINAL_STATES)


def test_only_dead_jobs_are_refunded() -> None:
    for state in JobState:
        assert job_view(state.value, "x", None, set()).refunded is (state is JobState.DEAD)


def test_a_running_job_names_its_step() -> None:
    view = job_view("synthesizing", None, None, set())
    assert view.step is JobState.SYNTHESIZING
    assert view.label == "Running: synthesizing"


def test_only_a_ready_job_is_downloadable_and_unknown_kinds_are_dropped() -> None:
    view = job_view("completed", None, None, {"report", "annotated_manuscript", "brief"})
    assert view.downloadable
    assert [k.value for k in view.artifacts] == ["report", "annotated_manuscript"]
    assert not job_view("completed", None, None, set()).downloadable


NOW = datetime(2026, 11, 11, tzinfo=UTC)


@pytest.mark.parametrize(
    ("owner", "expires", "status"),
    [
        (None, None, LeaseStatus.FREE),
        ("w1", NOW + timedelta(seconds=60), LeaseStatus.HELD),
        (None, NOW + timedelta(seconds=60), LeaseStatus.BACKING_OFF),
        ("w1", NOW - timedelta(seconds=1), LeaseStatus.EXPIRED),
        (None, NOW - timedelta(seconds=1), LeaseStatus.EXPIRED),
    ],
)
def test_lease_status_follows_the_claim_rule(
    owner: str | None, expires: datetime | None, status: LeaseStatus
) -> None:
    assert lease_status(owner, expires, NOW) is status


def test_the_cli_row_carries_the_view_and_the_lease() -> None:
    job = SimpleNamespace(
        id=uuid4(),
        state="pending",
        error="timed out",
        failure_kind=None,
        attempt=2,
        lease_owner=None,
        lease_expires_at=NOW + timedelta(minutes=2),
    )
    row = job_row(job, set(), NOW)
    assert row["phase"] == "retrying"
    assert row["lease"] == "backing_off"
    assert row["attempt"] == 2
