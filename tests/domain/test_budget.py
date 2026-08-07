import pytest

from editta.domain.budget import check_budget, estimate_cost_usd
from editta.domain.errors import BudgetExceeded


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
