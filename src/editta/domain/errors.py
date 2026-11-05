"""Typed exception hierarchy. No silent catches anywhere in the pipeline (PLAN.md §6).

Each error class declares whether retrying the same job can plausibly succeed. The
retry policy (domain/states.py) reads this flag — it never inspects messages or types
ad hoc, so adding a new failure mode means choosing its retryability in one place.
"""

from typing import ClassVar


class EdittaError(Exception):
    """Base for all Editta domain/application errors."""

    retryable: ClassVar[bool] = False


class ParseError(EdittaError):
    """Manuscript file failed validation or text extraction. Same bytes, same result."""


class LLMError(EdittaError):
    """LLM call failed after retries, or the circuit breaker is open. Usually transient."""

    retryable: ClassVar[bool] = True


class LLMRequestRejected(LLMError):
    """The provider rejected the request itself (4xx: invalid params, auth, too large).
    Deterministic — retrying the same request fails the same way."""

    retryable: ClassVar[bool] = False


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
