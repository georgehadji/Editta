"""Quote anchoring: every finding must be verbatim-traceable to the source text.

Mitigates both prompt-injection-steered and hallucinated findings (PLAN.md §5).
"""

from rapidfuzz import fuzz

QUOTE_MATCH_THRESHOLD = 0.9


def is_quote_anchored(
    quote: str, source_text: str, *, threshold: float = QUOTE_MATCH_THRESHOLD
) -> bool:
    if not quote.strip():
        return False
    return fuzz.partial_ratio(quote, source_text) / 100 >= threshold
