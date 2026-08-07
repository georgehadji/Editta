"""Typed exception hierarchy. No silent catches anywhere in the pipeline (PLAN.md §6)."""


class EdittaError(Exception):
    """Base for all Editta domain/application errors."""


class ParseError(EdittaError):
    """Manuscript file failed validation or text extraction."""


class LLMError(EdittaError):
    """LLM call failed after retries, or the circuit breaker is open."""


class BudgetExceeded(EdittaError):
    """Projected job cost exceeds the configured ceiling."""

    def __init__(self, projected_usd: float, ceiling_usd: float) -> None:
        super().__init__(f"projected cost ${projected_usd:.2f} exceeds ceiling ${ceiling_usd:.2f}")
        self.projected_usd = projected_usd
        self.ceiling_usd = ceiling_usd
