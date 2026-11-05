"""Quote anchoring: every finding an editor sees must be verbatim-traceable to the source.

Mitigates both prompt-injection-steered and hallucinated findings (PLAN.md §5). Applied
to every analysis pass's findings. Synthesis never produces findings of its own — it
cites these anchored ones by id (domain/synthesis.py) — so this is the single gate.
"""

from collections.abc import Iterable

from rapidfuzz import fuzz

from editta.domain.models import Finding

QUOTE_MATCH_THRESHOLD = 0.9


def is_quote_anchored(
    quote: str, source_text: str, *, threshold: float = QUOTE_MATCH_THRESHOLD
) -> bool:
    if not quote.strip():
        return False
    return fuzz.partial_ratio(quote, source_text) / 100 >= threshold


def anchor_findings(findings: Iterable[Finding], source_text: str) -> list[Finding]:
    """Keep only findings whose quote occurs in the source text."""
    return [f for f in findings if is_quote_anchored(f.quote, source_text)]
