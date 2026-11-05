from editta.domain.models import (
    Finding,
    LLMUsage,
    PassResult,
    Severity,
    SynthesisPlan,
    SynthesisPlanSection,
)
from editta.domain.synthesis import catalog_findings, finding_id, resolve_synthesis

USAGE = LLMUsage(input_tokens=1, output_tokens=1)


def _finding(quote: str, issue: str = "issue") -> Finding:
    return Finding(section="s", quote=quote, issue=issue, severity=Severity.MINOR, suggestion="x")


PASS_RESULTS = [
    PassResult(pass_name="language", findings=[_finding("alpha"), _finding("beta")], usage=USAGE),  # noqa: S106
    PassResult(pass_name="structure", findings=[_finding("gamma")], usage=USAGE),  # noqa: S106
]
CATALOG = catalog_findings(PASS_RESULTS)


def _plan(*sections: list[str]) -> SynthesisPlan:
    return SynthesisPlan(
        overview="overview",
        sections=[
            SynthesisPlanSection(title=f"t{i}", summary=f"s{i}", finding_ids=ids)
            for i, ids in enumerate(sections)
        ],
    )


def test_catalog_assigns_stable_ids_per_pass() -> None:
    assert list(CATALOG) == ["language-0", "language-1", "structure-0"]
    assert CATALOG[finding_id("structure", 0)].quote == "gamma"


def test_resolution_returns_the_original_findings_unchanged() -> None:
    report = resolve_synthesis(_plan(["language-1", "structure-0"]), CATALOG)
    assert report.sections[0].findings == [CATALOG["language-1"], CATALOG["structure-0"]]


def test_unknown_ids_are_dropped() -> None:
    """A hallucinated or injection-steered citation cannot become a finding."""
    report = resolve_synthesis(_plan(["language-0", "language-99", "made-up"]), CATALOG)
    assert report.sections[0].findings == [CATALOG["language-0"]]


def test_a_finding_cited_twice_appears_once() -> None:
    report = resolve_synthesis(_plan(["language-0"], ["language-0", "structure-0"]), CATALOG)
    assert report.sections[0].findings == [CATALOG["language-0"]]
    assert report.sections[1].findings == [CATALOG["structure-0"]]


def test_sections_and_prose_survive_even_when_empty() -> None:
    report = resolve_synthesis(_plan(["nope"]), CATALOG)
    assert report.overview == "overview"
    assert report.sections[0].summary == "s0" and report.sections[0].findings == []


def test_plan_schema_cannot_carry_findings() -> None:
    """The structural guarantee: the LLM's output type has ids, not Finding objects."""
    fields = SynthesisPlanSection.model_fields
    assert "findings" not in fields and "finding_ids" in fields
