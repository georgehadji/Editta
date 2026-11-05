from hypothesis import given
from hypothesis import strategies as st

from editta.domain.errors import (
    BudgetExceeded,
    LLMError,
    LLMRequestRejected,
    ManuscriptTooLong,
    ParseError,
)
from editta.domain.states import (
    MAX_ATTEMPTS,
    RETRY_MAX_DELAY_SECONDS,
    JobState,
    attempts_exhausted,
    can_transition,
    decide_after_failure,
    retry_delay_seconds,
    transition,
)


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


# --- retry policy ------------------------------------------------------------------


def test_retryable_failure_goes_back_to_pending_with_backoff() -> None:
    decision = decide_after_failure(attempts_made=1, retryable=True)
    assert decision.next_state is JobState.PENDING
    assert decision.attempts_made == 1
    assert decision.retry_after_seconds == 60
    assert not decision.is_terminal


def test_retryable_failure_on_last_attempt_is_dead() -> None:
    decision = decide_after_failure(attempts_made=MAX_ATTEMPTS, retryable=True)
    assert decision.next_state is JobState.DEAD
    assert decision.retry_after_seconds is None
    assert decision.is_terminal


def test_non_retryable_failure_is_dead_on_first_attempt() -> None:
    decision = decide_after_failure(attempts_made=1, retryable=False)
    assert decision.next_state is JobState.DEAD
    assert decision.attempts_made == 1


def test_backoff_doubles_and_is_capped() -> None:
    assert [retry_delay_seconds(n) for n in (1, 2, 3)] == [60, 120, 240]
    assert retry_delay_seconds(30) == RETRY_MAX_DELAY_SECONDS


@given(st.integers(min_value=1, max_value=50), st.booleans())
def test_retry_policy_always_reaches_a_legal_state(attempts: int, retryable: bool) -> None:
    decision = decide_after_failure(attempts_made=attempts, retryable=retryable)
    assert can_transition(JobState.FAILED, decision.next_state)
    assert decision.attempts_made == attempts
    assert (decision.retry_after_seconds is None) == decision.is_terminal
    if decision.retry_after_seconds is not None:
        assert 0 < decision.retry_after_seconds <= RETRY_MAX_DELAY_SECONDS


def test_error_classes_declare_retryability() -> None:
    assert LLMError.retryable
    assert not ParseError.retryable
    assert not BudgetExceeded.retryable
    assert not ManuscriptTooLong.retryable


def test_attempts_are_exhausted_only_past_the_limit() -> None:
    assert not attempts_exhausted(MAX_ATTEMPTS)
    assert attempts_exhausted(MAX_ATTEMPTS + 1)


def test_request_rejections_are_not_retryable() -> None:
    assert not LLMRequestRejected.retryable
    assert issubclass(LLMRequestRejected, LLMError)
