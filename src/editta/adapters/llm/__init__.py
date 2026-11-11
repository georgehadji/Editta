"""LLM client adapters: Anthropic direct, or the same models through OpenRouter."""

from editta.adapters.llm.client import AnthropicLLMClient
from editta.adapters.llm.openrouter import OpenRouterLLMClient
from editta.domain.ports import LLMClient


def build_llm_client(
    *,
    anthropic_api_key: str | None,
    openrouter_api_key: str | None,
    max_concurrency: int = 4,
    rate_limit_per_minute: int = 50,
) -> LLMClient:
    """OpenRouter when its key is set (Anthropic's key, if also set, only counts tokens,
    which is free); otherwise Anthropic direct. `rate_limit_per_minute` is this process's
    share of the organisation's limit: the bucket is not shared between workers."""
    anthropic = (
        AnthropicLLMClient(
            anthropic_api_key,
            max_concurrency=max_concurrency,
            rate_limit_per_minute=rate_limit_per_minute,
        )
        if anthropic_api_key
        else None
    )
    if openrouter_api_key:
        return OpenRouterLLMClient(
            openrouter_api_key,
            token_counter=anthropic,
            max_concurrency=max_concurrency,
            rate_limit_per_minute=rate_limit_per_minute,
        )
    if anthropic is None:
        raise ValueError("set EDITTA_OPENROUTER_API_KEY or EDITTA_ANTHROPIC_API_KEY")
    return anthropic
