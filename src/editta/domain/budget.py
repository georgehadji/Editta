"""Token/cost estimation and per-job budget guardrail. Pricing: claude-opus-5 (PLAN.md §4).

Pure functions only. The projection runs *before* any billable LLM call, from the
`count_tokens` preflight, so an over-budget job is refused without spending anything.
"""

from editta.domain.errors import BudgetExceeded, ManuscriptTooLong

INPUT_USD_PER_MTOK = 5.0
OUTPUT_USD_PER_MTOK = 25.0
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1
MAX_MANUSCRIPT_TOKENS = 700_000

# Conservative per-call output allowance for projection only (findings JSON + adaptive
# thinking at high effort). Real usage is recorded per pass; this just has to not
# under-estimate badly. It is deliberately below the client's max_tokens ceiling.
ASSUMED_OUTPUT_TOKENS_PER_CALL = 10_000
# Synthesis carries the four passes' findings JSON after the cached prefix, uncached.
ASSUMED_SYNTHESIS_EXTRA_INPUT_TOKENS = 20_000


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


def project_remaining_cost_usd(
    *,
    prefix_tokens: int,
    analysis_calls_remaining: int,
    include_synthesis: bool = True,
    output_tokens_per_call: int = ASSUMED_OUTPUT_TOKENS_PER_CALL,
) -> float:
    """Projected cost of the calls a job still has to make.

    The first remaining call writes the shared prefix to the cache (1.25x); every later
    call reads it (0.1x). On a resumed job the first remaining call re-writes the
    prefix, because a cache written by a previous attempt has almost certainly expired.
    """
    if analysis_calls_remaining < 0:
        raise ValueError("analysis_calls_remaining must be >= 0")
    calls = analysis_calls_remaining + (1 if include_synthesis else 0)
    if calls == 0:
        return 0.0
    return estimate_cost_usd(
        cache_write_tokens=prefix_tokens,
        cache_read_tokens=prefix_tokens * (calls - 1),
        plain_input_tokens=ASSUMED_SYNTHESIS_EXTRA_INPUT_TOKENS if include_synthesis else 0,
        output_tokens=output_tokens_per_call * calls,
    )


def check_budget(projected_usd: float, ceiling_usd: float) -> None:
    if projected_usd > ceiling_usd:
        raise BudgetExceeded(projected_usd, ceiling_usd)


def check_token_ceiling(token_count: int, max_tokens: int = MAX_MANUSCRIPT_TOKENS) -> None:
    if token_count > max_tokens:
        raise ManuscriptTooLong(token_count, max_tokens)
