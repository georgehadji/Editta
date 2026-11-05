"""LLM client adapters: Anthropic direct, or the same models through OpenRouter."""

from editta.adapters.llm.client import AnthropicLLMClient
from editta.adapters.llm.openrouter import OpenRouterLLMClient
from editta.domain.ports import LLMClient


def build_llm_client(
    *, anthropic_api_key: str | None, openrouter_api_key: str | None, max_concurrency: int = 4
) -> LLMClient:
    """OpenRouter when its key is set (Anthropic's key, if also set, only counts tokens,
    which is free); otherwise Anthropic direct."""
    anthropic = (
        AnthropicLLMClient(anthropic_api_key, max_concurrency=max_concurrency)
        if anthropic_api_key
        else None
    )
    if openrouter_api_key:
        return OpenRouterLLMClient(
            openrouter_api_key, token_counter=anthropic, max_concurrency=max_concurrency
        )
    if anthropic is None:
        raise ValueError("set EDITTA_OPENROUTER_API_KEY or EDITTA_ANTHROPIC_API_KEY")
    return anthropic
