"""FakeLLM adapter for pipeline tests — no network calls, deterministic output."""

import re
from collections.abc import Callable
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
    SynthesisPlan,
    SynthesisPlanSection,
)

T = TypeVar("T", bound=BaseModel)

FailureRule = Callable[[PromptParts], BaseException | None]


class FakeLLMClient:
    """Deterministic LLM fake.

    * Analysis passes return one finding quoting the manuscript's first four words.
    * Synthesis cites every finding id it was shown in one section, plus any
      `extra_synthesis_ids` (to test that unknown/hallucinated ids are dropped).
    * `fail_when` lets a test make specific calls raise.
    """

    def __init__(
        self,
        manuscript_text: str,
        *,
        fail_when: FailureRule | None = None,
        extra_synthesis_ids: list[str] | None = None,
    ) -> None:
        self._manuscript_text = manuscript_text
        self._fail_when = fail_when
        self._extra_synthesis_ids = extra_synthesis_ids or []
        self.calls: list[tuple[str, CachePolicy]] = []
        self.prompts: list[PromptParts] = []
        self.efforts: list[str] = []
        self.token_counts = 0

    async def count_tokens(self, parts: PromptParts) -> int:
        self.token_counts += 1
        return len(parts.manuscript_block) // 4

    async def analyze(
        self, parts: PromptParts, schema: type[T], *, cache: CachePolicy, effort: str
    ) -> LLMResult[T]:
        self.calls.append((parts.instructions[:20], cache))
        self.prompts.append(parts)
        self.efforts.append(effort)
        if self._fail_when is not None and (error := self._fail_when(parts)) is not None:
            raise error
        usage = LLMUsage(
            input_tokens=100,
            output_tokens=50,
            cache_write_tokens=100 if cache is CachePolicy.WRITE else 0,
            cache_read_tokens=100 if cache is CachePolicy.READ else 0,
        )
        output: BaseModel
        if schema is SynthesisPlan:
            shown_ids = re.findall(r'"id": "([^"]+)"', parts.instructions)
            output = SynthesisPlan(
                overview="fake overview",
                sections=[
                    SynthesisPlanSection(
                        title="Language",
                        summary="fake summary",
                        finding_ids=[*shown_ids, *self._extra_synthesis_ids],
                    )
                ],
            )
        else:
            output = PassFindings(findings=[self._finding()])
        return LLMResult(output=cast(T, output), usage=usage, model="fake-model")

    def _finding(self) -> Finding:
        return Finding(
            section="intro",
            quote=" ".join(self._manuscript_text.split()[:4]),  # whole words, like a model
            issue="fake issue",
            severity=Severity.MINOR,
            suggestion="fake suggestion",
        )
