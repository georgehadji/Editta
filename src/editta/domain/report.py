"""Pure report assembly from pass results — no I/O."""

from datetime import UTC, datetime

from editta.domain.models import PassResult, Report, SynthesisReport


def assemble_report(
    *,
    manuscript_filename: str,
    pass_results: list[PassResult],
    synthesis: SynthesisReport,
    now: datetime | None = None,
) -> Report:
    return Report(
        manuscript_filename=manuscript_filename,
        generated_at=now or datetime.now(UTC),
        synthesis=synthesis,
        pass_results=tuple(pass_results),
    )
