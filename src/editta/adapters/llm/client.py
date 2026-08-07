"""Caching-aware Anthropic client wrapper with structured outputs (PLAN.md §4).

# ponytail: output_config/thinking-effort wiring here follows the current Anthropic docs
# (platform.claude.com/docs/en/build-with-claude/{structured-outputs,extended-thinking}) but
# has not been smoke-tested against a live API key. Verify the exact `messages.parse(...)`
# kwarg shape against the installed `anthropic` SDK version before the first real eval run.
"""

import asyncio
from typing import Any, TypeVar

from anthropic import AsyncAnthropic
from pydantic import BaseModel

from editta.adapters.llm.resilience import CircuitBreaker, TokenBucket
from editta.domain.errors import LLMError
from editta.domain.models import CachePolicy, LLMResult, LLMUsage, PromptParts

T = TypeVar("T", bound=BaseModel)

MODEL = "claude-opus-5"
MAX_OUTPUT_TOKENS = 8192


def build_user_content(parts: PromptParts, cache: CachePolicy) -> list[dict[str, Any]]:
    """Manuscript body gets its own content block so cache_control applies to it alone."""
    manuscript_block: dict[str, Any] = {"type": "text", "text": parts.manuscript_block}
    if cache is not CachePolicy.NONE:
        manuscript_block["cache_control"] = {"type": "ephemeral"}
    return [manuscript_block, {"type": "text", "text": parts.instructions}]


class AnthropicLLMClient:
    """Adapter implementing the LLMClient port. Retries are SDK-native (429/5xx)."""

    def __init__(
        self, api_key: str, *, max_concurrency: int = 4, rate_limit_per_minute: int = 50
    ) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._breaker = CircuitBreaker()
        self._bucket = TokenBucket(
            capacity=rate_limit_per_minute, refill_per_second=rate_limit_per_minute / 60
        )

    async def count_tokens(self, parts: PromptParts) -> int:
        # SDK content-block TypedDicts are stricter than the plain dicts the API itself
        # accepts at runtime; ignore is scoped to that mismatch, not the call semantics.
        content = build_user_content(parts, CachePolicy.NONE)
        response = await self._client.messages.count_tokens(
            model=MODEL,
            system=parts.system,
            messages=[{"role": "user", "content": content}],  # type: ignore[typeddict-item]
        )
        return int(response.input_tokens)

    async def analyze(
        self, parts: PromptParts, schema: type[T], *, cache: CachePolicy, effort: str
    ) -> LLMResult[T]:
        self._breaker.before_call()
        async with self._semaphore:
            await self._bucket.acquire()
            content = build_user_content(parts, cache)
            try:
                message = await self._client.messages.parse(
                    model=MODEL,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    system=parts.system,
                    messages=[{"role": "user", "content": content}],  # type: ignore[typeddict-item]
                    thinking={"type": "adaptive"},
                    output_config={"effort": effort},  # type: ignore[arg-type]
                    output_format=schema,
                )
            except Exception as exc:
                self._breaker.record_failure()
                raise LLMError(f"LLM call failed: {exc}") from exc
        self._breaker.record_success()
        usage = message.usage
        return LLMResult(
            output=message.parsed_output,
            usage=LLMUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            ),
            model=MODEL,
        )
