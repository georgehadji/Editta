"""Synthesis by reference: the LLM groups pass findings by id; the domain resolves ids.

Why by reference rather than "copy the findings, then check them": a synthesis that
copies findings can alter an `issue` or `suggestion` while keeping a valid quote, or
use a quote so short it matches anywhere — quote-checking cannot catch either. When
the model can only *cite* ids, every finding in the report is byte-for-byte an
anchored pass finding, and "synthesis adds no new findings" is structural, not a
prompt instruction. Unknown ids (hallucinated or injection-steered) are dropped.

The overview and section summaries remain free text: they are prose for the editor,
not findings, and are presented as such.
"""

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from editta.domain.models import (
    Finding,
    PassResult,
    SynthesisPlan,
    SynthesisReport,
    SynthesisSection,
)


def finding_id(pass_name: str, index: int) -> str:
    return f"{pass_name}-{index}"


def catalog_findings(pass_results: Sequence[PassResult]) -> Mapping[str, Finding]:
    """Stable ids for every (already anchored) pass finding, in pass order."""
    return MappingProxyType(
        {
            finding_id(result.pass_name, index): finding
            for result in pass_results
            for index, finding in enumerate(result.findings)
        }
    )


def resolve_synthesis(plan: SynthesisPlan, catalog: Mapping[str, Finding]) -> SynthesisReport:
    """Turn the model's plan into the report. Unknown ids are dropped; a finding cited
    in several sections appears once, in the first. Empty sections keep their summary."""
    seen: set[str] = set()
    sections: list[SynthesisSection] = []
    for section in plan.sections:
        findings: list[Finding] = []
        for fid in section.finding_ids:
            if fid in catalog and fid not in seen:
                seen.add(fid)
                findings.append(catalog[fid])
        sections.append(
            SynthesisSection(title=section.title, summary=section.summary, findings=findings)
        )
    return SynthesisReport(overview=plan.overview, sections=sections)
