"""FakeLLM adapter for pipeline tests — no network calls, deterministic output."""

from typing import TypeVar, cast

from pydantic import BaseModel

from editta.domain.models import (
    CachePolicy,
    Finding,
    LLMResult,
    LLMUsage,
    PassFindings,
    PromptParts,
    Severity,
    SynthesisReport,
    SynthesisSection,
)

T = TypeVar("T", bound=BaseModel)


class FakeLLMClient:
    def __init__(self, manuscript_text: str) -> None:
        self._manuscript_text = manuscript_text
        self.calls: list[tuple[str, CachePolicy]] = []

    async def count_tokens(self, parts: PromptParts) -> int:
        return len(parts.manuscript_block) // 4

    async def analyze(
        self, parts: PromptParts, schema: type[T], *, cache: CachePolicy, effort: str
    ) -> LLMResult[T]:
        self.calls.append((parts.instructions[:20], cache))
        usage = LLMUsage(
            input_tokens=100,
            output_tokens=50,
            cache_write_tokens=100 if cache is CachePolicy.WRITE else 0,
            cache_read_tokens=100 if cache is CachePolicy.READ else 0,
        )
        output: BaseModel
        if schema is SynthesisReport:
            output = SynthesisReport(
                overview="fake overview",
                sections=[SynthesisSection(title="Language", summary="fake summary", findings=[])],
            )
        else:
            quote = self._manuscript_text[:20]
            output = PassFindings(
                findings=[
                    Finding(
                        section="intro",
                        quote=quote,
                        issue="fake issue",
                        severity=Severity.MINOR,
                        suggestion="fake suggestion",
                    )
                ]
            )
        return LLMResult(output=cast(T, output), usage=usage, model="fake-model")
