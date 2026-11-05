import pytest

from editta.domain.budget import (
    ASSUMED_OUTPUT_TOKENS_PER_CALL,
    ASSUMED_SYNTHESIS_EXTRA_INPUT_TOKENS,
    HAIKU,
    OPUS,
    SONNET,
    check_budget,
    check_token_ceiling,
    estimate_cost_usd,
    project_remaining_cost_usd,
)
from editta.domain.errors import BudgetExceeded, ManuscriptTooLong


def test_estimate_cost_uses_the_model_price() -> None:
    # 175k-token manuscript on Opus 5.5: 1 cache write + 1 cache read + 20k output.
    cost = estimate_cost_usd(
        model=OPUS,
        cache_write_tokens=175_000,
        cache_read_tokens=175_000,
        plain_input_tokens=0,
        output_tokens=20_000,
    )
    assert cost == pytest.approx((175_000 * 1.25 * 4.0 + 175_000 * 0.20 + 20_000 * 20.0) / 1e6)
    haiku = estimate_cost_usd(
        model=HAIKU,
        cache_write_tokens=0,
        cache_read_tokens=0,
        plain_input_tokens=175_000,
        output_tokens=20_000,
    )
    assert haiku < cost / 5


def test_check_budget_passes_within_ceiling() -> None:
    check_budget(projected_usd=5.0, ceiling_usd=6.0)


def test_check_budget_raises_over_ceiling() -> None:
    with pytest.raises(BudgetExceeded) as exc_info:
        check_budget(projected_usd=7.0, ceiling_usd=6.0)
    assert exc_info.value.projected_usd == 7.0


ROUTED = [OPUS, OPUS, HAIKU, SONNET]


def test_projection_for_a_fresh_job_shares_the_cache_only_within_a_model() -> None:
    # 175k prefix: the Opus pair writes then reads one cache; lone Haiku and Sonnet
    # passes pay plain input; synthesis has its own schema, so it pays plain input too.
    projected = project_remaining_cost_usd(
        prefix_tokens=175_000, analysis_models=ROUTED, synthesis_model=SONNET
    )
    out = ASSUMED_OUTPUT_TOKENS_PER_CALL
    expected = (
        estimate_cost_usd(
            model=OPUS,
            cache_write_tokens=175_000,
            cache_read_tokens=175_000,
            plain_input_tokens=0,
            output_tokens=out * 2,
        )
        + estimate_cost_usd(
            model=HAIKU,
            cache_write_tokens=0,
            cache_read_tokens=0,
            plain_input_tokens=175_000,
            output_tokens=out,
        )
        + estimate_cost_usd(
            model=SONNET,
            cache_write_tokens=0,
            cache_read_tokens=0,
            plain_input_tokens=175_000 + 175_000 + ASSUMED_SYNTHESIS_EXTRA_INPUT_TOKENS,
            output_tokens=out * 2,
        )
    )
    assert projected == pytest.approx(expected)
    assert projected < 6.0  # the default ceiling still admits a typical Greek essay


def test_projection_for_a_resumed_job_counts_only_remaining_calls() -> None:
    fresh = project_remaining_cost_usd(
        prefix_tokens=175_000, analysis_models=ROUTED, synthesis_model=SONNET
    )
    resumed = project_remaining_cost_usd(
        prefix_tokens=175_000, analysis_models=[OPUS], synthesis_model=SONNET
    )
    synthesis_only = project_remaining_cost_usd(
        prefix_tokens=175_000, analysis_models=[], synthesis_model=SONNET
    )
    assert fresh > resumed > synthesis_only > 0


def test_projection_without_any_remaining_calls_is_free() -> None:
    assert (
        project_remaining_cost_usd(prefix_tokens=175_000, analysis_models=[], synthesis_model=None)
        == 0.0
    )


def test_token_ceiling_reports_tokens_not_dollars() -> None:
    with pytest.raises(ManuscriptTooLong, match="800,000 tokens; the limit is 700,000"):
        check_token_ceiling(800_000)
