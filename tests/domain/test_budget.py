import pytest

from editta.domain.budget import (
    ASSUMED_OUTPUT_TOKENS_PER_CALL,
    ASSUMED_SYNTHESIS_EXTRA_INPUT_TOKENS,
    check_budget,
    check_token_ceiling,
    estimate_cost_usd,
    project_remaining_cost_usd,
)
from editta.domain.errors import BudgetExceeded, ManuscriptTooLong


def test_estimate_cost_matches_plan_ballpark() -> None:
    # ~175k-token manuscript: 1 cache write + 4 cache reads + ~25k output (PLAN.md §4).
    cost = estimate_cost_usd(
        cache_write_tokens=175_000,
        cache_read_tokens=175_000 * 4,
        plain_input_tokens=0,
        output_tokens=25_000,
    )
    assert 1.9 < cost < 2.2


def test_check_budget_passes_within_ceiling() -> None:
    check_budget(projected_usd=5.0, ceiling_usd=6.0)


def test_check_budget_raises_over_ceiling() -> None:
    with pytest.raises(BudgetExceeded) as exc_info:
        check_budget(projected_usd=7.0, ceiling_usd=6.0)
    assert exc_info.value.projected_usd == 7.0


def test_projection_for_a_fresh_job_prices_synthesis_uncached() -> None:
    # 175k prefix: pass 1 writes the cache, passes 2-4 read it; synthesis has its own
    # output schema, which invalidates the cache, so it pays full input price.
    projected = project_remaining_cost_usd(prefix_tokens=175_000, analysis_calls_remaining=4)
    expected = estimate_cost_usd(
        cache_write_tokens=175_000,
        cache_read_tokens=175_000 * 3,
        plain_input_tokens=175_000 + ASSUMED_SYNTHESIS_EXTRA_INPUT_TOKENS,
        output_tokens=ASSUMED_OUTPUT_TOKENS_PER_CALL * 5,
    )
    assert projected == pytest.approx(expected)
    assert projected < 6.0  # the default ceiling still admits a typical Greek essay


def test_synthesis_only_resume_writes_no_cache() -> None:
    projected = project_remaining_cost_usd(prefix_tokens=100_000, analysis_calls_remaining=0)
    expected = estimate_cost_usd(
        cache_write_tokens=0,
        cache_read_tokens=0,
        plain_input_tokens=100_000 + ASSUMED_SYNTHESIS_EXTRA_INPUT_TOKENS,
        output_tokens=ASSUMED_OUTPUT_TOKENS_PER_CALL,
    )
    assert projected == pytest.approx(expected)


def test_projection_for_a_resumed_job_counts_only_remaining_calls() -> None:
    fresh = project_remaining_cost_usd(prefix_tokens=175_000, analysis_calls_remaining=4)
    resumed = project_remaining_cost_usd(prefix_tokens=175_000, analysis_calls_remaining=1)
    synthesis_only = project_remaining_cost_usd(prefix_tokens=175_000, analysis_calls_remaining=0)
    assert fresh > resumed > synthesis_only > 0


def test_projection_without_any_remaining_calls_is_free() -> None:
    assert (
        project_remaining_cost_usd(
            prefix_tokens=175_000, analysis_calls_remaining=0, include_synthesis=False
        )
        == 0.0
    )


def test_token_ceiling_reports_tokens_not_dollars() -> None:
    with pytest.raises(ManuscriptTooLong, match="800,000 tokens; the limit is 700,000"):
        check_token_ceiling(800_000)
