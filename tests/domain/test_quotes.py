import random

import pytest

from editta.domain.models import Finding, Severity
from editta.domain.quotes import (
    anchor_findings,
    is_quote_anchored,
    locate_quote,
    normalize_text,
    normalize_with_map,
)

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


# --- locate_quote / normalize_with_map: where the quote is, by the same rules ------------

_TRICKY = [
    "  Στο δεύτερο   κεφάλαιο\nυπάρχει ένα λάθος ",
    "Straße ß ﬁne «Είναι» – “x”",
    "Αθήνα é text-\n  ing here",
    " lead\ttrail \n",
    "",
    "   ",
    "ΐ ΰ ŉ",
    "a-\nb",
    "x - \n y",
]


@pytest.mark.parametrize("text", _TRICKY)
def test_the_map_gives_the_same_text_as_normalize_text(text: str) -> None:
    mapped = normalize_with_map(text)
    assert mapped.text == normalize_text(text)
    assert len(mapped.starts) == len(mapped.ends) == len(mapped.text)


def test_the_map_agrees_with_normalize_text_on_random_input() -> None:
    rnd = random.Random(7)  # noqa: S311 -- test data, not security
    alphabet = "aβγ ΑΣς\n\t-«»–ßﬁ́ .e"
    for _ in range(2000):
        text = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 30)))
        assert normalize_with_map(text).text == normalize_text(text), repr(text)


def _located(quote: str, source: str) -> str | None:
    span = locate_quote(quote, normalize_with_map(source))
    return None if span is None else source[span.start : span.end]


def test_locate_returns_the_original_characters_not_the_normalized_ones() -> None:
    source = "Στο  δεύτερο\nκεφάλαιο υπάρχει ένα λάθος εδώ."
    assert _located("δεύτερο κεφάλαιο", source) == "δεύτερο\nκεφάλαιο"


def test_locate_sees_through_a_hyphen_at_a_line_break() -> None:
    assert _located("text-\ning", "foo text-\ning bar") == "text-\ning"


def test_locate_folds_quotes_and_case_like_anchoring_does() -> None:
    assert _located('"Είναι σαφές"', "«Είναι σαφές» εδώ") == "Είναι σαφές"


def test_locate_refuses_a_short_quote_that_occurs_twice() -> None:
    assert _located("λάθος", "ένα λάθος και άλλο λάθος") is None


def test_locate_follows_a_long_near_match_to_one_place() -> None:
    source = "Αρχή. Ένα μακρύ απόσπασμα που έχει ένα λάθος εδώ. Τέλος."
    found = _located("Ένα μακρύ απόσπασμα που έχει ένα λαθος εδώ", source)
    assert found is not None
    assert "απόσπασμα" in found


@pytest.mark.parametrize("quote", ["", "   ", "δεν υπάρχει πουθενά στο κείμενο αυτό το απόσπασμα"])
def test_locate_gives_none_for_empty_or_absent_quotes(quote: str) -> None:
    assert _located(quote, "Στο δεύτερο κεφάλαιο υπάρχει ένα λάθος") is None


def test_anchored_and_locatable_never_disagree() -> None:
    source = "Στο δεύτερο κεφάλαιο υπάρχει ένα λάθος. Και ένα ακόμη λάθος εδώ. Ναι, ναι."
    mapped = normalize_with_map(source)
    quotes = [
        "λάθος",
        "ένα ακόμη λάθος εδώ",
        "ναι",
        "Στο δεύτερο κεφάλαιο υπάρχει ένα λαθος",
        "ψευδές",
    ]
    for quote in quotes:
        located = locate_quote(quote, mapped) is not None
        assert is_quote_anchored(quote, source) == located, quote
