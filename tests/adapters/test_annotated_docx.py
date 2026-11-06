"""The commented copy of the author's file. The safety property that matters: the author's
text and formatting come back exactly as they went in; only comments are added."""

import io
import zipfile
from collections.abc import Sequence

import docx
import pytest
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from lxml import etree

from editta.adapters.export.annotated_docx import (
    MAX_COMMENT_CHARS,
    AnnotatedManuscriptExporter,
    comment_text,
)
from editta.adapters.parsing.docx import extract_docx
from editta.domain.artifacts import ExportContext
from editta.domain.models import (
    Finding,
    LLMUsage,
    ManuscriptText,
    PassResult,
    Severity,
    SynthesisReport,
)
from tests.adapters.pdf_factory import text_pdf

_W = nsdecls("w")


def _finding(quote: str, issue: str = "issue", suggestion: str = "fix") -> Finding:
    return Finding(
        section="s", quote=quote, issue=issue, severity=Severity.MAJOR, suggestion=suggestion
    )


def _save(document: "docx.document.Document") -> bytes:
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _context(source: bytes, findings: Sequence[Finding]) -> ExportContext:
    return ExportContext(
        manuscript=extract_docx(source, "book.docx"),
        source=source,
        pass_results=(
            PassResult(
                pass_name="language",  # noqa: S106 -- trips the hardcoded-password heuristic
                findings=list(findings),
                usage=LLMUsage(input_tokens=1, output_tokens=1),
            ),
        ),
        synthesis=SynthesisReport(overview="o", sections=[]),
    )


def _plain_doc(*paragraphs: str) -> bytes:
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    return _save(document)


def _all_text(data: bytes) -> str:
    """Every w:t in document order: the strictest view of "the text did not change"."""
    root = docx.Document(io.BytesIO(data)).element
    return "".join(t.text or "" for t in root.iter(qn("w:t")))


def _commented_text(paragraph_xml: etree._Element) -> list[str]:
    """For each comment range in the paragraph, the text between its start and end."""
    ranges: dict[str, list[str]] = {}
    open_ids: list[str] = []
    for node in paragraph_xml.iter():
        if node.tag == qn("w:commentRangeStart"):
            open_ids.append(node.get(qn("w:id")))
            ranges[node.get(qn("w:id"))] = []
        elif node.tag == qn("w:commentRangeEnd"):
            open_ids.remove(node.get(qn("w:id")))
        elif node.tag == qn("w:t"):
            for comment_id in open_ids:
                ranges[comment_id].append(node.text or "")
    return ["".join(parts) for parts in ranges.values()]


def test_the_comment_sits_on_exactly_the_quoted_words() -> None:
    source = _plain_doc("Η γλώσσα έχει ένα σφάλμα εδώ.", "Άλλη παράγραφος.")
    out = AnnotatedManuscriptExporter().build(_context(source, [_finding("ένα σφάλμα")]))

    assert out is not None
    document = docx.Document(io.BytesIO(out))
    assert _commented_text(document.paragraphs[0]._p) == ["ένα σφάλμα"]
    (comment,) = list(document.comments)
    assert comment.author == "Editta"
    assert "issue" in comment.text
    assert "fix" in comment.text


def test_the_authors_text_comes_back_unchanged() -> None:
    source = _plain_doc("Η γλώσσα έχει ένα σφάλμα εδώ.", "  Με κενά στην αρχή.  ", "Τέλος εδώ.")
    findings = [_finding("ένα σφάλμα"), _finding("Με κενά στην αρχή"), _finding("Τέλος")]
    out = AnnotatedManuscriptExporter().build(_context(source, findings))

    assert out is not None
    assert _all_text(out) == _all_text(source)
    assert extract_docx(out, "book.docx").text == extract_docx(source, "book.docx").text


def test_a_comment_in_a_paragraph_with_leading_spaces_still_hits_its_words() -> None:
    source = _plain_doc("   Με κενά στην αρχή και ένα λάθος.")
    out = AnnotatedManuscriptExporter().build(_context(source, [_finding("ένα λάθος")]))

    assert out is not None
    paragraph = docx.Document(io.BytesIO(out)).paragraphs[0]._p
    assert _commented_text(paragraph) == ["ένα λάθος"]


def test_both_halves_of_a_cut_run_keep_the_authors_formatting() -> None:
    document = docx.Document()
    run = document.add_paragraph().add_run("Πριν ένα σφάλμα μετά")
    run.bold = True
    run.font.name = "Garamond"
    out = AnnotatedManuscriptExporter().build(_context(_save(document), [_finding("ένα σφάλμα")]))

    assert out is not None
    runs = [r for r in docx.Document(io.BytesIO(out)).paragraphs[0].runs if r.text]
    assert [r.text for r in runs] == ["Πριν ", "ένα σφάλμα", " μετά"]
    assert all(r.bold and r.font.name == "Garamond" for r in runs)


def test_a_quote_over_several_runs_cuts_only_the_outer_edges() -> None:
    document = docx.Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("Πριν ένα ")
    paragraph.add_run("μεγάλο").bold = True
    paragraph.add_run(" σφάλμα μετά")
    source = _save(document)
    out = AnnotatedManuscriptExporter().build(_context(source, [_finding("ένα μεγάλο σφάλμα")]))

    assert out is not None
    paragraph_out = docx.Document(io.BytesIO(out)).paragraphs[0]
    assert _commented_text(paragraph_out._p) == ["ένα μεγάλο σφάλμα"]
    middle = [r for r in paragraph_out.runs if r.text == "μεγάλο"]
    assert len(middle) == 1
    assert middle[0].bold
    assert _all_text(out) == _all_text(source)


def test_several_findings_in_one_paragraph_each_get_their_own_range() -> None:
    source = _plain_doc("Ένα πρώτο λάθος, και πιο κάτω ένα δεύτερο σφάλμα εδώ.")
    findings = [_finding("πρώτο λάθος"), _finding("δεύτερο σφάλμα"), _finding("Ένα πρώτο")]
    out = AnnotatedManuscriptExporter().build(_context(source, findings))

    assert out is not None
    paragraph = docx.Document(io.BytesIO(out)).paragraphs[0]
    assert sorted(_commented_text(paragraph._p)) == sorted(
        ["πρώτο λάθος", "δεύτερο σφάλμα", "Ένα πρώτο"]
    )
    assert _all_text(out) == _all_text(source)


def _c14n(data: bytes) -> bytes:
    return etree.tostring(etree.fromstring(data), method="c14n")


def test_only_comment_parts_and_the_document_change_in_the_package() -> None:
    source = _plain_doc("Η γλώσσα έχει ένα σφάλμα εδώ.")
    out = AnnotatedManuscriptExporter().build(_context(source, [_finding("ένα σφάλμα")]))
    assert out is not None

    before, after = zipfile.ZipFile(io.BytesIO(source)), zipfile.ZipFile(io.BytesIO(out))
    assert set(before.namelist()) <= set(after.namelist())
    assert all("comments" in name for name in set(after.namelist()) - set(before.namelist()))

    allowed = {"word/document.xml", "word/_rels/document.xml.rels", "[Content_Types].xml"}
    for name in before.namelist():
        if name in allowed:
            continue
        old, new = before.read(name), after.read(name)
        if name.endswith((".xml", ".rels")):  # same XML, however the library wrote it
            old, new = _c14n(old), _c14n(new)
        assert old == new, f"{name} changed"


def test_the_authors_own_comments_are_kept() -> None:
    document = docx.Document()
    paragraph = document.add_paragraph("Η γλώσσα έχει ένα σφάλμα εδώ και μια σημείωση.")
    document.add_comment(paragraph.runs, text="σημείωση του συγγραφέα", author="Συγγραφέας")
    out = AnnotatedManuscriptExporter().build(_context(_save(document), [_finding("ένα σφάλμα")]))

    assert out is not None
    comments = list(docx.Document(io.BytesIO(out)).comments)
    assert sorted(c.author for c in comments) == ["Editta", "Συγγραφέας"]
    assert any("σημείωση του συγγραφέα" in c.text for c in comments)


def test_text_inside_a_hyperlink_is_not_commented() -> None:
    document = docx.Document()
    paragraph = document.add_paragraph("Δείτε ")
    link = (
        f'<w:hyperlink {_W} w:anchor="x"><w:r><w:t>έναν σύνδεσμο λάθους</w:t></w:r></w:hyperlink>'
    )
    paragraph._p.append(parse_xml(link))
    paragraph.add_run(" και ένα άλλο σφάλμα εδώ.")
    source = _save(document)
    findings = [_finding("έναν σύνδεσμο λάθους"), _finding("άλλο σφάλμα")]
    out = AnnotatedManuscriptExporter().build(_context(source, findings))

    assert out is not None
    paragraph_out = docx.Document(io.BytesIO(out)).paragraphs[0]._p
    assert _commented_text(paragraph_out) == ["άλλο σφάλμα"]
    assert _all_text(out) == _all_text(source)


def test_text_inside_a_field_is_not_commented() -> None:
    document = docx.Document()
    paragraph = document.add_paragraph("Σελίδα ")
    for xml in (
        f'<w:r {_W}><w:fldChar w:fldCharType="begin"/></w:r>',
        f"<w:r {_W}><w:instrText> PAGE </w:instrText></w:r>",
        f'<w:r {_W}><w:fldChar w:fldCharType="separate"/></w:r>',
        f"<w:r {_W}><w:t>αριθμός λάθους</w:t></w:r>",
        f'<w:r {_W}><w:fldChar w:fldCharType="end"/></w:r>',
    ):
        paragraph._p.append(parse_xml(xml))
    paragraph.add_run(" και ένα σφάλμα εδώ.")
    source = _save(document)
    findings = [_finding("αριθμός λάθους"), _finding("ένα σφάλμα")]
    out = AnnotatedManuscriptExporter().build(_context(source, findings))

    assert out is not None
    paragraph_out = docx.Document(io.BytesIO(out)).paragraphs[0]._p
    assert _commented_text(paragraph_out) == ["ένα σφάλμα"]
    assert _all_text(out) == _all_text(source)


def test_a_run_with_a_tab_is_not_cut_but_may_be_commented_whole() -> None:
    document = docx.Document()
    document.add_paragraph().add_run("Πριν\tένα σφάλμα εδώ")  # python-docx makes the \t a w:tab
    source = _save(document)

    # the edge would fall inside a run that holds more than plain text
    assert AnnotatedManuscriptExporter().build(_context(source, [_finding("ένα σφάλμα")])) is None

    whole = AnnotatedManuscriptExporter().build(
        _context(source, [_finding("Πριν\tένα σφάλμα εδώ")])
    )
    assert whole is not None
    assert _all_text(whole) == _all_text(source)


def test_a_quote_across_two_paragraphs_is_left_out() -> None:
    source = _plain_doc("Πρώτη παράγραφος με ένα σφάλμα", "συνεχίζει στη δεύτερη παράγραφο εδώ.")
    quote = "ένα σφάλμα συνεχίζει στη δεύτερη παράγραφο"
    assert AnnotatedManuscriptExporter().build(_context(source, [_finding(quote)])) is None


def test_nothing_placed_means_no_file() -> None:
    source = _plain_doc("Μια παράγραφος χωρίς τίποτα το ύποπτο.")
    assert AnnotatedManuscriptExporter().build(_context(source, [_finding("δεν υπάρχει")])) is None


def test_a_pdf_gets_no_commented_file() -> None:
    context = ExportContext(
        manuscript=ManuscriptText(source_filename="book.pdf", text="A line of text"),
        source=text_pdf([["A line of text with an error here."]]),
        pass_results=(),
        synthesis=SynthesisReport(overview="o", sections=[]),
    )
    assert AnnotatedManuscriptExporter().build(context) is None


def test_a_file_that_no_longer_matches_the_analysed_text_gets_no_comments() -> None:
    source = _plain_doc("Η γλώσσα έχει ένα σφάλμα εδώ.")
    context = _context(source, [_finding("ένα σφάλμα")])
    drifted = context.manuscript.model_copy(update={"text": context.manuscript.text + "!"})
    stale = ExportContext(
        manuscript=drifted,
        source=source,
        pass_results=context.pass_results,
        synthesis=context.synthesis,
    )
    assert AnnotatedManuscriptExporter().build(stale) is None


def test_a_broken_file_yields_no_artifact_instead_of_an_error() -> None:
    context = ExportContext(
        manuscript=ManuscriptText(source_filename="book.docx", text="x"),
        source=b"PK\x03\x04 not really a docx",
        pass_results=(),
        synthesis=SynthesisReport(overview="o", sections=[]),
    )
    assert AnnotatedManuscriptExporter().build(context) is None


@pytest.mark.parametrize("bad", ["\x00", "\x08", "\x0b", "\x1f", "\ud800", "￾"])
def test_characters_xml_forbids_never_reach_the_comment(bad: str) -> None:
    text = comment_text("language", _finding("q", issue=f"a{bad}b", suggestion=f"c{bad}d"))
    assert bad not in text
    assert "ab" in text
    assert "cd" in text


def test_markup_in_model_text_stays_text() -> None:
    source = _plain_doc("Η γλώσσα έχει ένα σφάλμα εδώ.")
    finding = _finding("ένα σφάλμα", issue="<w:p>&amp;</w:p>")
    out = AnnotatedManuscriptExporter().build(_context(source, [finding]))

    assert out is not None
    (comment,) = list(docx.Document(io.BytesIO(out)).comments)
    assert "<w:p>&amp;</w:p>" in comment.text


def test_an_overlong_comment_is_cut_and_the_file_stays_valid() -> None:
    source = _plain_doc("Η γλώσσα έχει ένα σφάλμα εδώ.")
    out = AnnotatedManuscriptExporter().build(
        _context(source, [_finding("ένα σφάλμα", issue="x\x00y" * 3000)])
    )

    assert out is not None
    (comment,) = list(docx.Document(io.BytesIO(out)).comments)
    assert len(comment.text) <= MAX_COMMENT_CHARS + 200  # the cap plus the fixed note


def test_the_comment_names_the_pass_the_severity_and_that_it_is_machine_made() -> None:
    text = comment_text("language", _finding("q", issue="πρόβλημα", suggestion="διόρθωση"))
    assert text.startswith("[major] Γλώσσα — πρόβλημα")
    assert "Πρόταση: διόρθωση" in text
    assert "Αυτόματη" in text
