"""Orchestrates parse -> cache-prime pass1 -> gather passes2-4 -> synthesize -> export.

Cache priming order matters: the cache only becomes readable after the first response
begins, so pass 1 is awaited alone before the rest fan out and read the cached prefix
at 0.1x cost instead of paying full input price 4 more times (PLAN.md §4).
"""

import asyncio
from typing import cast

from editta.adapters.parsing.docx import parse_docx
from editta.domain.budget import check_budget, check_token_ceiling, estimate_cost_usd
from editta.domain.models import CachePolicy, ManuscriptText, PassFindings, PassResult, Report
from editta.domain.ports import LLMClient
from editta.domain.quotes import is_quote_anchored
from editta.domain.report import assemble_report
from editta.passes.base import AnalysisPass
from editta.passes.integrity import IntegrityPass
from editta.passes.language import LanguagePass
from editta.passes.metadata import MetadataPass
from editta.passes.structure import StructurePass
from editta.passes.synthesis import SynthesisPass

ANALYSIS_PASSES = (LanguagePass(), StructurePass(), MetadataPass(), IntegrityPass())

# Conservative per-call output ceiling used only for the pre-run budget projection.
_ASSUMED_OUTPUT_TOKENS_PER_CALL = 6_000


async def run_pipeline(
    *, manuscript_bytes: bytes, filename: str, llm: LLMClient, cost_ceiling_usd: float
) -> Report:
    manuscript = parse_docx(manuscript_bytes, filename)

    preflight_tokens = await llm.count_tokens(ANALYSIS_PASSES[0].build_prompt(manuscript))
    check_token_ceiling(preflight_tokens)

    first_pass, *rest_passes = ANALYSIS_PASSES
    first_result = await _run_pass(first_pass, manuscript, llm, cache=CachePolicy.WRITE)

    _check_projected_budget(
        first_result, remaining_calls=len(rest_passes) + 1, ceiling_usd=cost_ceiling_usd
    )

    rest_results = list(
        await asyncio.gather(
            *(_run_pass(p, manuscript, llm, cache=CachePolicy.READ) for p in rest_passes)
        )
    )
    pass_results = [first_result, *rest_results]

    synthesis_pass = SynthesisPass()
    synthesis_prompt = synthesis_pass.build_prompt(manuscript, pass_results)
    synthesis_result = await llm.analyze(
        synthesis_prompt,
        synthesis_pass.output_schema,
        cache=CachePolicy.READ,
        effort=synthesis_pass.effort,
    )

    return assemble_report(
        manuscript_filename=filename,
        pass_results=pass_results,
        synthesis=synthesis_result.output,
    )


async def _run_pass(
    analysis_pass: AnalysisPass, manuscript: ManuscriptText, llm: LLMClient, *, cache: CachePolicy
) -> PassResult:
    prompt = analysis_pass.build_prompt(manuscript)
    result = await llm.analyze(
        prompt, analysis_pass.output_schema, cache=cache, effort=analysis_pass.effort
    )
    # Concrete passes always use PassFindings as their output_schema (base.py's Protocol
    # only guarantees BaseModel); synthesis is handled separately and never goes through here.
    findings = cast(PassFindings, result.output).findings
    anchored = [f for f in findings if is_quote_anchored(f.quote, manuscript.text)]
    return PassResult(pass_name=analysis_pass.name, findings=anchored, usage=result.usage)


def _check_projected_budget(
    first_result: PassResult, *, remaining_calls: int, ceiling_usd: float
) -> None:
    cache_write = first_result.usage.cache_write_tokens or first_result.usage.input_tokens
    projected = estimate_cost_usd(
        cache_write_tokens=cache_write,
        cache_read_tokens=cache_write * remaining_calls,
        plain_input_tokens=0,
        output_tokens=_ASSUMED_OUTPUT_TOKENS_PER_CALL * (remaining_calls + 1),
    )
    check_budget(projected, ceiling_usd)
