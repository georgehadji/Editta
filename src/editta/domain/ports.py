"""Behavioral contracts (ports). Phase 2 adds JobQueue/BlobStore here."""

from typing import Protocol, TypeVar

from pydantic import BaseModel

from editta.domain.models import CachePolicy, LLMResult, PromptParts

T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    async def analyze(
        self, parts: PromptParts, schema: type[T], *, cache: CachePolicy, effort: str
    ) -> LLMResult[T]: ...

    async def count_tokens(self, parts: PromptParts) -> int: ...
