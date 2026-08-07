from editta.domain.states import JobState, can_transition, transition


def test_linear_happy_path_allowed() -> None:
    assert can_transition(JobState.PENDING, JobState.PARSING)
    assert can_transition(JobState.EXPORTING, JobState.COMPLETED)


def test_any_non_terminal_can_fail_or_cancel() -> None:
    assert can_transition(JobState.ANALYZING, JobState.FAILED)
    assert can_transition(JobState.SYNTHESIZING, JobState.CANCELLED)


def test_terminal_states_have_no_outgoing_transitions() -> None:
    assert not can_transition(JobState.COMPLETED, JobState.PENDING)
    assert not can_transition(JobState.CANCELLED, JobState.PENDING)


def test_failed_can_retry_or_go_dead() -> None:
    assert can_transition(JobState.FAILED, JobState.PENDING)
    assert can_transition(JobState.DEAD, JobState.PENDING) is False


def test_illegal_transition_raises() -> None:
    try:
        transition(JobState.PENDING, JobState.COMPLETED)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for skipping states")


def test_skipping_states_is_illegal() -> None:
    import pytest

    with pytest.raises(ValueError, match="illegal job transition"):
        transition(JobState.PENDING, JobState.EXPORTING)
