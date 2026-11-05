"""Caching-aware Anthropic client wrapper with structured outputs (PLAN.md §4).

# ponytail: output_config/thinking-effort wiring here follows the current Anthropic docs
# (platform.claude.com/docs/en/build-with-claude/{structured-outputs,extended-thinking}) but
# has not been smoke-tested against a live API key. Verify the exact `messages.parse(...)`
# kwarg shape against the installed `anthropic` SDK version before the first real eval run.
"""

import asyncio
from typing import Final, Literal, TypeVar, cast, get_args

from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types import MessageParam, OutputConfigParam, TextBlockParam
from pydantic import BaseModel

from editta.adapters.llm.resilience import CircuitBreaker, TokenBucket
from editta.domain.errors import LLMError, LLMRequestRejected
from editta.domain.models import CachePolicy, LLMResult, LLMUsage, PromptParts

T = TypeVar("T", bound=BaseModel)

# Must cover adaptive thinking at high effort *plus* the structured findings JSON; at
# 8192 a long thinking phase could truncate the JSON and fail the parse, and at 16k a
# book-length part did (calls stream, so the SDK's non-streaming ceiling no longer
# applies). A ceiling only: output is billed as generated.
MAX_OUTPUT_TOKENS: Final = 64_000

Effort = Literal["low", "medium", "high", "xhigh", "max"]
_EFFORTS: Final = frozenset(get_args(Effort))


def build_user_content(parts: PromptParts, cache: CachePolicy) -> list[TextBlockParam]:
    """Manuscript body gets its own content block so cache_control applies to it alone.

    WRITE and READ are identical on the wire — the API decides hit or miss from the
    prefix; the policy only records the caller's intent (and drives tests/metrics).
    """
    manuscript_block: TextBlockParam = {"type": "text", "text": parts.manuscript_block}
    if cache is not CachePolicy.NONE:
        manuscript_block["cache_control"] = {"type": "ephemeral"}
    return [manuscript_block, {"type": "text", "text": parts.instructions}]


def _messages(parts: PromptParts, cache: CachePolicy) -> list[MessageParam]:
    return [{"role": "user", "content": build_user_content(parts, cache)}]


# Request-level rejections: the same request will fail the same way on retry, and they
# say nothing about provider health, so they neither retry nor trip the breaker.
# (408 timeout, 409, 429 rate limit and 5xx/529 overload stay retryable.)
_NON_RETRYABLE_STATUSES: Final = frozenset({400, 401, 403, 404, 413, 422})


def _output_config(effort: str) -> OutputConfigParam:
    if effort not in _EFFORTS:
        raise LLMRequestRejected(f"unsupported effort level: {effort!r}")
    return {"effort": cast(Effort, effort)}


def _is_rejection(exc: Exception) -> bool:
    return isinstance(exc, APIStatusError) and exc.status_code in _NON_RETRYABLE_STATUSES


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

    async def count_tokens(self, parts: PromptParts, *, model: str) -> int:
        try:
            response = await self._client.messages.count_tokens(
                model=model, system=parts.system, messages=_messages(parts, CachePolicy.NONE)
            )
        except Exception as exc:
            if _is_rejection(exc):
                raise LLMRequestRejected(f"token count rejected: {exc}") from exc
            raise LLMError(f"token count failed: {exc}") from exc
        return int(response.input_tokens)

    async def analyze(
        self,
        parts: PromptParts,
        schema: type[T],
        *,
        cache: CachePolicy,
        effort: str,
        model: str,
        fallbacks: tuple[str, ...] = (),  # Anthropic-only: nothing to fall back to
    ) -> LLMResult[T]:
        output_config = _output_config(effort)
        self._breaker.before_call()
        async with self._semaphore:
            await self._bucket.acquire()
            try:
                # Streamed: a book-length non-streamed call is dropped by the network
                # long before Opus answers.
                async with self._client.messages.stream(
                    model=model,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    system=parts.system,
                    messages=_messages(parts, cache),
                    # Fixed for every call: the thinking config is part of the cache key
                    # (see passes/base.py), so it must never vary between passes.
                    # For the same reason passes must share one `output_format`.
                    thinking={"type": "adaptive"},
                    output_config=output_config,
                    output_format=schema,
                ) as stream:
                    message = await stream.get_final_message()
            except Exception as exc:
                if _is_rejection(exc):
                    raise LLMRequestRejected(f"LLM request rejected: {exc}") from exc
                self._breaker.record_failure()
                raise LLMError(f"LLM call failed: {exc}") from exc
        self._breaker.record_success()
        if message.parsed_output is None:
            # A refusal or a max_tokens cut-off yields no parseable output. Retryable:
            # adaptive thinking length varies between attempts.
            raise LLMError(f"model returned no structured output (stop: {message.stop_reason})")
        usage = message.usage
        return LLMResult(
            output=message.parsed_output,
            usage=LLMUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            ),
            model=model,
        )
