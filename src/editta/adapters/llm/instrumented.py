"""One log line per model call: model, tokens, how long, how it ended.

A Decorator over the LLMClient port, like `_RecordFailedSpend` in services/jobs.py, so it
works for every provider and needs no vendor code. Numbers only: never the prompt or the
answer, which quote the manuscript.
"""

import time
from typing import TypeVar

import structlog
from pydantic import BaseModel

from editta.domain.models import CachePolicy, LLMResult, LLMUsage, PromptParts
from editta.domain.ports import LLMClient

T = TypeVar("T", bound=BaseModel)

logger = structlog.get_logger(__name__)


class InstrumentedLLMClient:
    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner

    async def count_tokens(self, parts: PromptParts, *, model: str) -> int:
        return await self._inner.count_tokens(parts, model=model)

    async def analyze(
        self,
        parts: PromptParts,
        schema: type[T],
        *,
        cache: CachePolicy,
        effort: str,
        model: str,
        fallbacks: tuple[str, ...] = (),
    ) -> LLMResult[T]:
        started = time.monotonic()
        outcome = "ok"
        served = model
        usage: LLMUsage | None = None
        try:
            result = await self._inner.analyze(
                parts, schema, cache=cache, effort=effort, model=model, fallbacks=fallbacks
            )
        except BaseException as exc:  # logged and re-raised: cancellation included
            outcome = type(exc).__name__
            usage = getattr(exc, "usage", None)  # what a billed but unusable call cost
            raise
        else:
            served, usage = result.model, result.usage
            return result
        finally:
            logger.info(
                "llm.call",
                model=model,
                served_model=served,
                outcome=outcome,
                duration_ms=round((time.monotonic() - started) * 1000),
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                cache_read_tokens=usage.cache_read_tokens if usage else None,
                cache_write_tokens=usage.cache_write_tokens if usage else None,
            )
