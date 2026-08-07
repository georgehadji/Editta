import io
from datetime import UTC, datetime

import docx

from editta.adapters.export.docx_builder import ReportDocxBuilder
from editta.domain.models import (
    Finding,
    LLMUsage,
    PassResult,
    Report,
    Severity,
    SynthesisReport,
    SynthesisSection,
)

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
