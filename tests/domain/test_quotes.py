from editta.domain.models import Finding, Severity
from editta.domain.quotes import anchor_findings, is_quote_anchored

SOURCE = "Η γλώσσα του κειμένου παρουσιάζει επαναλήψεις στο δεύτερο κεφάλαιο."


def test_exact_quote_is_anchored() -> None:
    assert is_quote_anchored("επαναλήψεις στο δεύτερο κεφάλαιο", SOURCE)


def test_fabricated_quote_is_not_anchored() -> None:
    assert not is_quote_anchored("αυτό το απόσπασμα δεν υπάρχει πουθενά στο κείμενο", SOURCE)


def test_empty_quote_is_not_anchored() -> None:
    assert not is_quote_anchored("   ", SOURCE)


def _finding(quote: str) -> Finding:
    return Finding(section="ch2", quote=quote, issue="i", severity=Severity.MINOR, suggestion="s")


def test_anchor_findings_drops_fabricated_quotes() -> None:
    real, fake = _finding("επαναλήψεις στο δεύτερο"), _finding("δεν υπάρχει πουθενά εδώ μέσα")
    assert anchor_findings([real, fake], SOURCE) == [real]
