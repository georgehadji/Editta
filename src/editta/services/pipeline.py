"""In-memory pipeline for the CLI: parse -> preflight -> analyze -> synthesize -> report.

A thin composition of services/analysis.py with no persistence. The worker's
JobRunner composes the same core with checkpointing, so both entrypoints share one
implementation of budget, cache ordering and quote anchoring.
"""

from editta.adapters.parsing import parse_manuscript
from editta.domain.models import Report
from editta.domain.ports import LLMClient
from editta.domain.report import assemble_report
from editta.services.analysis import ANALYSIS_PASSES, preflight, run_passes, run_synthesis


async def run_pipeline(
    *, manuscript_bytes: bytes, filename: str, llm: LLMClient, cost_ceiling_usd: float
) -> Report:
    manuscript = parse_manuscript(manuscript_bytes, filename)
    await preflight(
        llm,
        manuscript,
        analysis_passes_remaining=len(ANALYSIS_PASSES),
        cost_ceiling_usd=cost_ceiling_usd,
    )
    pass_results = await run_passes(ANALYSIS_PASSES, manuscript, llm)
    synthesis = await run_synthesis(manuscript, pass_results, llm)
    return assemble_report(
        manuscript_filename=filename, pass_results=pass_results, synthesis=synthesis
    )
