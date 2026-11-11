"""Typed exception hierarchy. No silent catches anywhere in the pipeline (PLAN.md §6).

Each error class declares whether retrying the same job can plausibly succeed. The
retry policy (domain/states.py) reads this flag — it never inspects messages or types
ad hoc, so adding a new failure mode means choosing its retryability in one place.
"""

from typing import ClassVar

from editta.domain.models import LLMUsage


class EdittaError(Exception):
    """Base for all Editta domain/application errors."""

    retryable: ClassVar[bool] = False


class ParseError(EdittaError):
    """Manuscript file failed validation or text extraction. Same bytes, same result."""


class LLMError(EdittaError):
    """LLM call failed after retries, or the circuit breaker is open. Usually transient.

    Messages end up in job records and API responses, so they must never contain model
    output or vendor exception text that could quote the manuscript. `usage` is what a
    call that was billed but unusable cost.
    """

    retryable: ClassVar[bool] = True

    def __init__(self, message: str, *, usage: LLMUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class LLMRequestRejected(LLMError):
    """The provider rejected the request itself (4xx: invalid params, auth, too large).
    Deterministic — retrying the same request fails the same way."""

    retryable: ClassVar[bool] = False


class LLMOutputInvalid(LLMError):
    """The model answered, but not with output that fits the schema (a refusal, say).
    Retryable: another attempt may answer differently."""


class LLMOutputTruncated(LLMError):
    """The answer hit the output-token ceiling. The same prompt fills the ceiling again,
    and every try is billed in full, so retrying only burns money."""

    retryable: ClassVar[bool] = False


class LLMTimeout(LLMError):
    """A model call, or the whole analysis phase, ran past its time limit. Retryable:
    the next attempt resumes from the checkpoints, so finished passes are not paid again."""


class LeaseLost(EdittaError):
    """This worker no longer owns the job (its lease expired and another worker claimed
    it). Not a job failure: the runner stops writing and records nothing."""


class BudgetExceeded(EdittaError):
    """Projected job cost exceeds the configured ceiling. Deterministic for a given input."""

    def __init__(self, projected_usd: float, ceiling_usd: float) -> None:
        super().__init__(f"projected cost ${projected_usd:.2f} exceeds ceiling ${ceiling_usd:.2f}")
        self.projected_usd = projected_usd
        self.ceiling_usd = ceiling_usd


class ManuscriptTooLong(BudgetExceeded):
    """Manuscript exceeds the token ceiling. A BudgetExceeded so callers that guard on
    cost also guard on size, but with a message in tokens rather than fake dollars."""

    def __init__(self, token_count: int, max_tokens: int) -> None:
        EdittaError.__init__(
            self, f"manuscript is {token_count:,} tokens; the limit is {max_tokens:,}"
        )
        self.projected_usd = float("inf")
        self.ceiling_usd = float("nan")
        self.token_count = token_count
        self.max_tokens = max_tokens


class InsufficientCredits(EdittaError):
    """The tenant cannot pay for another job. Raised at upload, before anything is
    stored or queued."""

    def __init__(self, balance: int, required: int) -> None:
        super().__init__(f"not enough credits: {balance} available, {required} required")
        self.balance = balance
        self.required = required


class NotFound(EdittaError):
    """The thing asked for does not exist for this tenant. Delivery layers map it to 404."""


class JobNotFound(NotFound):
    def __init__(self) -> None:
        super().__init__("job not found")


class ReportNotReady(NotFound):
    def __init__(self) -> None:
        super().__init__("report not ready")


class PermissionDenied(EdittaError):
    """The authenticated user's role does not allow this action."""
