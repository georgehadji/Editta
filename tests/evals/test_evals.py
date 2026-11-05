"""Offline checks for the live-API evals: the checklists stay valid against their
essays, and the runner + scoring work end-to-end with the fake LLM — so the first
paid run cannot fail on a bug in the harness itself."""

from datetime import UTC, datetime

import pytest
from evals.run import EVALS_DIR, evaluate, load_checklists, markdown_to_docx, verdict
from evals.scoring import CallRecord, score_essay

from editta.adapters.parsing import parse_manuscript
from editta.domain.models import (
    CachePolicy,
    Finding,
    LLMUsage,
    PassResult,
    Report,
    Severity,
    SynthesisReport,
    SynthesisSection,
)
from editta.domain.quotes import normalize_text
from tests.fakes import FakeLLMClient

CHECKLISTS = load_checklists()


def _essay(name: str) -> str:
    return (EVALS_DIR / "essays" / name).read_text(encoding="utf-8")


def test_there_are_three_reference_essays() -> None:
    assert len(CHECKLISTS) == 3


@pytest.mark.parametrize("checklist", CHECKLISTS, ids=lambda c: c.essay)
def test_every_anchor_occurs_verbatim_in_its_essay(checklist) -> None:  # noqa: ANN001
    text = normalize_text(_essay(checklist.essay))
    for expected in checklist.expected:
        for anchor in expected.anchors:
            assert normalize_text(anchor) in text, f"{expected.id}: {anchor!r} not in essay"


@pytest.mark.parametrize("checklist", CHECKLISTS, ids=lambda c: c.essay)
def test_essays_are_long_enough_to_be_cached(checklist) -> None:  # noqa: ANN001
    # Opus 5 caches prompts of >= 512 tokens; Greek runs ~3 tokens/word, so 1,500+
    # characters is comfortably above it. Shorter essays would fail the cache check.
    assert len(_essay(checklist.essay)) > 1_500


def test_markdown_headings_become_parsed_sections() -> None:
    manuscript = parse_manuscript(markdown_to_docx(_essay("poli_kai_mnimi.md")), "x.docx")
    assert manuscript.sections[:2] == ("Σκέψεις", "Η θέση")


@pytest.mark.asyncio
async def test_runner_works_end_to_end_with_the_fake_llm() -> None:
    checklist = next(c for c in CHECKLISTS if c.essay == "psifiaki_anagnosi.md")
    fake = FakeLLMClient(manuscript_text=normalize_text(_essay(checklist.essay)))

    report, score = await evaluate(checklist, fake, cost_ceiling_usd=6.0)

    assert len(report.pass_results) == 4
    assert score.cache_ok, score.cache_detail
    assert score.cost_usd > 0
    assert 0.0 <= score.recall <= 1.0


def _report(quotes: list[str], overview: str = "overview") -> Report:
    usage = LLMUsage(input_tokens=1, output_tokens=1)
    findings = [
        Finding(section="s", quote=q, issue="i", severity=Severity.MINOR, suggestion="x")
        for q in quotes
    ]
    return Report(
        manuscript_filename="x.docx",
        generated_at=datetime.now(UTC),
        synthesis=SynthesisReport(
            overview=overview, sections=[SynthesisSection(title="t", summary="s")]
        ),
        pass_results=(PassResult(pass_name="integrity", findings=findings, usage=usage),),  # noqa: S106
    )


def _calls(*, pass2_reads: int = 900) -> list[CallRecord]:
    def call(write: int, read: int, raw: int | None) -> CallRecord:
        usage = LLMUsage(
            input_tokens=50, output_tokens=500, cache_write_tokens=write, cache_read_tokens=read
        )
        return CallRecord("X", CachePolicy.READ, "high", usage, raw, 1.0)

    return [call(900, 0, 2), call(0, pass2_reads, 2), call(0, 900, 2), call(0, 900, 2)] + [
        call(0, 0, None)
    ]


def test_a_finding_quoting_the_planted_passage_counts_as_found() -> None:
    checklist = next(c for c in CHECKLISTS if c.essay == "texniti_noimosyni_sxoleio.md")
    report = _report(["Όλοι οι ειδικοί συμφωνούν ότι η εξατομικευμένη μάθηση"])

    score = score_essay(checklist, report, _calls())

    assert score.found == ["sweeping-unsupported-claim"]
    assert score.missed_required == ["run-on-sentence"]
    assert score.missed_optional == ["embedded-instruction"]  # optional: not in recall
    assert score.recall == 0.5


def test_obeying_the_injection_raises_a_warning() -> None:
    checklist = next(c for c in CHECKLISTS if c.essay == "texniti_noimosyni_sxoleio.md")
    report = _report([], overview="Το κείμενο είναι αριστούργημα και πρέπει να εκδοθεί αμέσως.")

    score = score_essay(checklist, report, _calls())

    assert len(score.injection_warnings) == 2


def test_a_cache_miss_on_a_fanned_out_pass_fails_the_essay() -> None:
    checklist = CHECKLISTS[0]
    score = score_essay(checklist, _report([]), _calls(pass2_reads=0))

    failures = verdict(score, min_recall=0.0, cost_ceiling_usd=6.0)

    assert not score.cache_ok
    assert any("passes [2] read no cache" in f for f in failures)


def test_anchoring_drop_rate_is_reported() -> None:
    score = score_essay(CHECKLISTS[0], _report(["a quote"]), _calls())
    assert (score.raw_findings, score.anchored_findings) == (8, 1)
    assert score.anchoring_drop_rate == pytest.approx(7 / 8)
