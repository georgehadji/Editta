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
    ALLOWED_FROM,
    MAX_ATTEMPTS,
    RETRY_MAX_DELAY_SECONDS,
    TERMINAL_STATES,
    JobState,
    attempts_exhausted,
    decide_after_failure,
    retry_delay_seconds,
)

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
    assert decision.next_state in (JobState.PENDING, JobState.DEAD)
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


# --- transitions ---------------------------------------------------------------------


def test_every_state_has_an_entry_in_the_transition_table() -> None:
    assert set(ALLOWED_FROM) == set(JobState)


def test_terminal_states_are_absorbing() -> None:
    for target, sources in ALLOWED_FROM.items():
        assert not sources & TERMINAL_STATES, f"{target} is reachable from a terminal state"


def test_every_unfinished_state_can_die_retry_or_resume() -> None:
    unfinished = set(JobState) - TERMINAL_STATES
    for target in (JobState.DEAD, JobState.PENDING, JobState.PARSING):
        assert ALLOWED_FROM[target] == unfinished


def test_the_pipeline_moves_forward_one_step_at_a_time() -> None:
    steps = [
        JobState.PARSING,
        JobState.ANALYZING,
        JobState.SYNTHESIZING,
        JobState.EXPORTING,
        JobState.COMPLETED,
    ]
    for previous, current in zip(steps, steps[1:], strict=False):
        assert ALLOWED_FROM[current] == {previous}
