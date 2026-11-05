"""Token/cost estimation and per-job budget guardrail. Pricing: PLAN.md §4.

Pure functions only. The projection runs *before* any billable LLM call, from the
`count_tokens` preflight, so an over-budget job is refused without spending anything.
"""

from collections import Counter
from collections.abc import Sequence
from typing import Final, NamedTuple

from editta.domain.errors import BudgetExceeded, ManuscriptTooLong

# Hard passes run on Opus, moderate ones on Sonnet, easy ones on Haiku (passes/).
OPUS: Final = "claude-opus-5-5"
SONNET: Final = "claude-sonnet-5-5"
HAIKU: Final = "claude-haiku-5-5"
# Fallbacks (passes/ fallback_models), reached through OpenRouter only.
GEMINI_ARGON: Final = "gemini-4-argon"
GEMINI_FLASH: Final = "gemini-3.8-flash"
GPT_SOL: Final = "gpt-6.1-sol"
GPT_TERRA: Final = "gpt-6.1-terra"


class ModelPricing(NamedTuple):
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_usd_per_mtok: float


PRICING: Final = {
    OPUS: ModelPricing(4.0, 20.0, 0.20),
    SONNET: ModelPricing(2.0, 10.0, 0.20),
    # shortcut: Haiku 5.5 is $0.10/$0.50 up to 100k prompt tokens; always pricing the
    # >100k tier over-projects short essays by cents. Price by prompt size if that matters.
    HAIKU: ModelPricing(0.50, 2.50, 0.05),
    GEMINI_ARGON: ModelPricing(2.0, 10.0, 0.10),
    GEMINI_FLASH: ModelPricing(0.75, 3.75, 0.075),
    # shortcut: GPT-6.1 prices rise above 272k prompt tokens; the higher tier is always
    # used, as a book-length Greek manuscript is past it.
    GPT_SOL: ModelPricing(4.0, 15.0, 0.20),
    GPT_TERRA: ModelPricing(2.0, 7.5, 0.10),
}
CACHE_WRITE_MULTIPLIER = 1.25
MAX_MANUSCRIPT_TOKENS = 700_000

# Conservative per-call output allowance for projection only (findings JSON + adaptive
# thinking at high effort). Real usage is recorded per pass; this just has to not
# under-estimate badly. It is deliberately below the client's max_tokens ceiling.
ASSUMED_OUTPUT_TOKENS_PER_CALL = 10_000
# Synthesis carries the four passes' findings JSON after the cached prefix, uncached.
ASSUMED_SYNTHESIS_EXTRA_INPUT_TOKENS = 20_000


def estimate_cost_usd(
    *,
    model: str,
    cache_write_tokens: int,
    cache_read_tokens: int,
    plain_input_tokens: int,
    output_tokens: int,
) -> float:
    price = PRICING[model]
    input_cost = (
        (cache_write_tokens * CACHE_WRITE_MULTIPLIER + plain_input_tokens)
        * price.input_usd_per_mtok
        + cache_read_tokens * price.cache_read_usd_per_mtok
    ) / 1_000_000
    output_cost = output_tokens / 1_000_000 * price.output_usd_per_mtok
    return input_cost + output_cost


def project_remaining_cost_usd(
    *,
    prefix_tokens: int,
    analysis_models: Sequence[str],
    synthesis_model: str | None,
    output_tokens_per_call: int = ASSUMED_OUTPUT_TOKENS_PER_CALL,
) -> float:
    """Projected cost of the calls a job still has to make (one entry per remaining
    analysis pass in `analysis_models`; `synthesis_model=None` if synthesis is done).

    Mirrors run_passes' cache policy. Prompt caches are per model, so passes group by
    model: in a group of two or more the first call writes the shared prefix (1.25x)
    and the rest read it; a lone pass sends no cache marker and pays plain input. On a
    resumed job the first call re-writes — an earlier attempt's cache has almost
    certainly expired (5 min TTL).

    shortcut: priced on each pass's primary model. A fallback runs only when that
    model fails, and pays plain input on its own cache, so a job that falls back can
    cost more than projected; recorded usage carries the model that served the call.

    Synthesis is priced at the full, uncached input rate: it uses a different
    structured-output schema, and per the Anthropic structured-outputs docs changing
    `output_config.format` invalidates the prompt cache. Pricing it as a cache read
    would under-project by ~0.9x the manuscript and let over-ceiling jobs through.
    """
    total = 0.0
    for model, calls in Counter(analysis_models).items():
        shared = calls > 1
        total += estimate_cost_usd(
            model=model,
            cache_write_tokens=prefix_tokens if shared else 0,
            cache_read_tokens=prefix_tokens * (calls - 1),
            plain_input_tokens=0 if shared else prefix_tokens,
            output_tokens=output_tokens_per_call * calls,
        )
    if synthesis_model is not None:
        total += estimate_cost_usd(
            model=synthesis_model,
            cache_write_tokens=0,
            cache_read_tokens=0,
            plain_input_tokens=prefix_tokens + ASSUMED_SYNTHESIS_EXTRA_INPUT_TOKENS,
            output_tokens=output_tokens_per_call,
        )
    return total


def check_budget(projected_usd: float, ceiling_usd: float) -> None:
    if projected_usd > ceiling_usd:
        raise BudgetExceeded(projected_usd, ceiling_usd)


def check_token_ceiling(token_count: int, max_tokens: int = MAX_MANUSCRIPT_TOKENS) -> None:
    if token_count > max_tokens:
        raise ManuscriptTooLong(token_count, max_tokens)
