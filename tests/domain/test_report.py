from datetime import UTC, datetime

from editta.domain.models import LLMUsage, PassResult, SynthesisReport
from editta.domain.report import assemble_report


def test_assemble_report_is_pure_and_deterministic() -> None:
    fixed_time = datetime(2026, 1, 1, tzinfo=UTC)
    usage = LLMUsage(input_tokens=100, output_tokens=50)
    pass_result = PassResult(pass_name="language", findings=[], usage=usage)
    synthesis = SynthesisReport(overview="ok", sections=[])

    report = assemble_report(
        manuscript_filename="essay.docx",
        pass_results=[pass_result],
        synthesis=synthesis,
        now=fixed_time,
    )

    assert report.generated_at == fixed_time
    assert report.pass_results == (pass_result,)
    assert not hasattr(report, "recommendation")
    assert not hasattr(report.synthesis, "score")
