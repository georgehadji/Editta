"""Frozen domain models: Manuscript, Finding, PassResult, Report."""

from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T", bound=BaseModel)


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Severity(StrEnum):
    """Triage aid for the editor's attention — never presented as an objective score."""

    MINOR = "minor"
    MODERATE = "moderate"
    MAJOR = "major"


class CachePolicy(StrEnum):
    """Prompt-cache behavior for a single LLM call (PLAN.md §4)."""

    WRITE = "write"
    READ = "read"
    NONE = "none"


class Finding(Frozen):
    section: str
    quote: str
    issue: str
    severity: Severity
    suggestion: str


class PassFindings(Frozen):
    """Structured-output shape shared by all four analysis passes."""

    findings: list[Finding] = Field(default_factory=list)


class SynthesisSection(Frozen):
    title: str
    summary: str
    findings: list[Finding] = Field(default_factory=list)


class SynthesisReport(Frozen):
    """Editorial synthesis. No recommendation/score field — product safety rail (PLAN.md §5)."""

    overview: str
    sections: list[SynthesisSection]


class ManuscriptText(Frozen):
    """Parsed, normalized manuscript ready for analysis."""

    source_filename: str
    text: str
    sections: tuple[str, ...] = ()


class PromptParts(Frozen):
    system: str
    manuscript_block: str
    instructions: str


class LLMUsage(Frozen):
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


class LLMResult(Frozen, Generic[T]):
    output: T
    usage: LLMUsage
    model: str


class PassResult(Frozen):
    pass_name: str
    findings: list[Finding]
    usage: LLMUsage


class Report(Frozen):
    manuscript_filename: str
    generated_at: datetime
    synthesis: SynthesisReport
    pass_results: tuple[PassResult, ...]
