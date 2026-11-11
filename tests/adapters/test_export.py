import io
from datetime import UTC, datetime

import docx
import pytest

from editta.adapters.export.docx_builder import ReportDocxBuilder
from editta.adapters.parsing import parse_manuscript
from editta.domain.models import (
    Finding,
    LLMUsage,
    PassResult,
    Report,
    Severity,
    SynthesisReport,
    SynthesisSection,
)
from editta.services.pipeline import run_pipeline
from tests.adapters.pdf_factory import text_pdf
from tests.fakes import FakeLLMClient

FINDING = Finding(
    section="intro",
    quote="a real quote",
    issue="unclear opening",
    severity=Severity.MINOR,
    suggestion="tighten it",
)

REPORT = Report(
    manuscript_filename="essay.docx",
    generated_at=datetime(2026, 1, 1, tzinfo=UTC),
    synthesis=SynthesisReport(
        overview="overall solid draft",
        sections=[SynthesisSection(title="Language", summary="a few nits", findings=[FINDING])],
    ),
    pass_results=(
        PassResult(
            pass_name="language",  # noqa: S106 -- trips the hardcoded-password heuristic
            findings=[FINDING],
            usage=LLMUsage(input_tokens=1, output_tokens=1),
        ),
    ),
)


def test_build_produces_valid_docx_with_no_score_or_verdict_text() -> None:
    docx_bytes = ReportDocxBuilder(REPORT).build()

    document = docx.Document(io.BytesIO(docx_bytes))
    full_text = "\n".join(p.text for p in document.paragraphs).lower()

    assert "essay.docx" in full_text
    assert "unclear opening" in full_text
    assert "a real quote" in full_text
    for banned in ("recommendation:", "verdict:", "score:", "accept", "reject"):
        assert banned not in full_text


def test_control_characters_in_model_text_do_not_fail_the_report() -> None:
    """The model's own words (not only the manuscript's) can carry characters XML forbids."""
    dirty = FINDING.model_copy(
        update={"issue": "bad\x0bissue", "suggestion": "fix\x00this", "section": "in\x1ftro"}
    )
    report = REPORT.model_copy(
        update={
            "synthesis": SynthesisReport(
                overview="over\x08view",
                sections=[
                    SynthesisSection(title="Lang\x0cuage", summary="a\x01b", findings=[dirty])
                ],
            ),
            "pass_results": (
                PassResult(
                    pass_name="language",  # noqa: S106 -- trips the hardcoded-password heuristic
                    findings=[dirty],
                    usage=LLMUsage(input_tokens=1, output_tokens=1),
                ),
            ),
        }
    )

    document = docx.Document(io.BytesIO(ReportDocxBuilder(report).build()))
    full_text = "\n".join(p.text for p in document.paragraphs)

    assert "badissue" in full_text
    assert "overview" in full_text


@pytest.mark.asyncio
async def test_report_for_a_pdf_with_control_characters_exports() -> None:
    """Regression (hunt D1): a control character in the PDF reached a quoted finding and
    made python-docx raise 'All strings must be XML compatible'."""
    pdf = text_pdf([["Broken\x01font words here and more text follows after that point."]])
    llm = FakeLLMClient(manuscript_text=parse_manuscript(pdf, "a.pdf").text)

    report = await run_pipeline(
        manuscript_bytes=pdf, filename="a.pdf", llm=llm, cost_ceiling_usd=6.0
    )

    assert ReportDocxBuilder(report).build()
