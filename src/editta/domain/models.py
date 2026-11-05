"""Frozen domain models: Manuscript, Finding, PassResult, Report."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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


class SynthesisPlanSection(Frozen):
    title: str
    summary: str
    finding_ids: list[str] = Field(default_factory=list)


class SynthesisPlan(Frozen):
    """What the synthesis LLM call returns: grouping and prose that *reference* pass
    findings by id. It cannot carry a Finding, so it cannot invent or alter one — the
    report's findings are always the anchored originals (domain/quotes.py).
    No recommendation/score field — product safety rail (PLAN.md §5)."""

    overview: str
    sections: list[SynthesisPlanSection]


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


class LLMResult[T: BaseModel](Frozen):
    output: T
    usage: LLMUsage
    model: str


class ClaimedJob(Frozen):
    """A job handed to one worker by JobQueue.claim(), lease-owned until it heartbeats.

    `attempt` is this run's 1-based attempt number: the claim itself increments it, so
    a worker that crashes mid-job still consumes an attempt and a crash-looping job
    is eventually dead-lettered. `lease_owner` is the fencing token: every state write
    the runner makes is conditional on still holding it.
    """

    job_id: UUID
    tenant_id: UUID
    manuscript_id: UUID
    attempt: int
    lease_owner: str


class PassResult(Frozen):
    pass_name: str
    findings: list[Finding]
    usage: LLMUsage
    model: str = ""


class Report(Frozen):
    manuscript_filename: str
    generated_at: datetime
    synthesis: SynthesisReport
    pass_results: tuple[PassResult, ...]


class JobOutcomeKind(StrEnum):
    COMPLETED = "completed"
    RETRY = "retry"
    DEAD = "dead"
    ABANDONED = "abandoned"  # lease lost to another worker; it owns the job now


class JobOutcome(Frozen):
    """Result of one JobRunner.run(). The worker maps it to a queue action — the runner
    itself never touches the queue, so job logic and lease mechanics stay decoupled."""

    kind: JobOutcomeKind
    error: str | None = None
    retry_after_seconds: int | None = None

    @classmethod
    def completed(cls) -> "JobOutcome":
        return cls(kind=JobOutcomeKind.COMPLETED)

    @classmethod
    def retry(cls, *, error: str, after_seconds: int) -> "JobOutcome":
        return cls(kind=JobOutcomeKind.RETRY, error=error, retry_after_seconds=after_seconds)

    @classmethod
    def dead(cls, *, error: str) -> "JobOutcome":
        return cls(kind=JobOutcomeKind.DEAD, error=error)

    @classmethod
    def abandoned(cls, *, error: str) -> "JobOutcome":
        return cls(kind=JobOutcomeKind.ABANDONED, error=error)
