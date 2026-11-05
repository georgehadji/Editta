import pytest

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


# --- short quotes: exact and unique; long quotes: fuzzy -------------------------------

BOOK = (
    "Το κείμενο εξετάζει τη γλώσσα και τη δομή και τη μορφή. "
    "Στο δεύτερο κεφάλαιο υπάρχει ένα λάθος: η λέξη κειμενο γράφεται χωρίς τόνο."
)


def test_a_unique_misspelled_word_still_anchors() -> None:
    """Single-word typo findings are legitimate and must survive."""
    assert is_quote_anchored("κειμενο", BOOK)


def test_a_common_word_does_not_anchor() -> None:
    """'και' occurs repeatedly, so it points at no particular passage."""
    assert BOOK.count("και") > 1
    assert not is_quote_anchored("και", BOOK)


def test_a_short_near_miss_does_not_anchor() -> None:
    """Regression: fuzzy matching let short quotes 'match' text that isn't there."""
    assert not is_quote_anchored("δεύτερο κεφάλιο", BOOK)


def test_a_long_quote_with_one_slip_still_anchors() -> None:
    assert is_quote_anchored("Στο δευτερο κεφάλαιο υπάρχει ένα λάθος", BOOK)


def test_line_breaks_from_pdf_extraction_do_not_break_anchoring() -> None:
    assert is_quote_anchored("Στο δεύτερο κεφάλαιο\nυπάρχει   ένα λάθος", BOOK)


def test_decomposed_greek_accents_match_precomposed_text() -> None:
    import unicodedata

    decomposed = unicodedata.normalize("NFD", "δεύτερο κεφάλαιο υπάρχει ένα")
    assert decomposed != "δεύτερο κεφάλαιο υπάρχει ένα"
    assert is_quote_anchored(decomposed, BOOK)


# --- normalization models routinely apply when quoting --------------------------------

GUILLEMETS = "Ο συγγραφέας γράφει: «Η πόλη θυμάται». Μετά προ-\nχωρά — αργά — στο ναι ή στο όχι."


@pytest.mark.parametrize(
    "quote",
    [
        '"Η πόλη θυμάται"',  # straight quotes for « »
        "η πόλη θυμάται",  # case
        "Η πόλη θυμάται.",  # trailing punctuation the text doesn't have there
        "προχωρά",  # word re-joined across a PDF line-break hyphen
        "αργά - στο ναι",  # hyphen for em dash
        "ναι",  # a word that also occurs inside "είναι"-like words elsewhere
    ],
)
def test_quotes_survive_typical_model_normalization(quote: str) -> None:
    assert is_quote_anchored(quote, GUILLEMETS + " Είναι σαφές.")


def test_dropped_accents_are_not_forgiven_for_short_quotes() -> None:
    """Accent errors are what Greek typo findings are about; folding them would make a
    misspelling indistinguishable from the correct word."""
    assert not is_quote_anchored("Αθηνα", "Ταξίδι στην Αθήνα.")
