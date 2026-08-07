"""Token/cost estimation and per-job budget guardrail. Pricing: claude-opus-5 (PLAN.md §4)."""

from editta.domain.errors import BudgetExceeded

INPUT_USD_PER_MTOK = 5.0
OUTPUT_USD_PER_MTOK = 25.0
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1
MAX_MANUSCRIPT_TOKENS = 700_000


def estimate_cost_usd(
    *,
    cache_write_tokens: int,
    cache_read_tokens: int,
    plain_input_tokens: int,
    output_tokens: int,
) -> float:
    weighted_input = (
        cache_write_tokens * CACHE_WRITE_MULTIPLIER
        + cache_read_tokens * CACHE_READ_MULTIPLIER
        + plain_input_tokens
    )
    input_cost = weighted_input / 1_000_000 * INPUT_USD_PER_MTOK
    output_cost = output_tokens / 1_000_000 * OUTPUT_USD_PER_MTOK
    return input_cost + output_cost


def check_budget(projected_usd: float, ceiling_usd: float) -> None:
    if projected_usd > ceiling_usd:
        raise BudgetExceeded(projected_usd, ceiling_usd)


def check_token_ceiling(token_count: int, max_tokens: int = MAX_MANUSCRIPT_TOKENS) -> None:
    if token_count > max_tokens:
        # Reuse BudgetExceeded: a too-long manuscript is a budget problem, not a parse error.
        raise BudgetExceeded(projected_usd=float(token_count), ceiling_usd=float(max_tokens))
