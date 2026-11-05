"""Shared analysis core: preflight -> prime cache -> fan out -> synthesize, all anchored.

The single implementation of "how a manuscript is analyzed", used by both entrypoints:
the CLI pipeline (services/pipeline.py, in-memory) and the worker's JobRunner
(services/jobs.py, checkpointed). Before this module each had its own copy, and the
copies drifted — the budget guardrail existed only on the CLI path. Persistence is
injected as an optional `on_pass_completed` hook (observer), so this module owns
ordering and invariants while callers own side effects.

Invariants enforced here, not by convention at call sites:
  * budget is checked *before* the first billable call (preflight token count);
  * passes are grouped by model (prompt caches are per model); in each group the
    first pass runs alone to write the cache and the rest fan out after it (the cache
    is readable only once a response has started — PLAN.md §4). Groups run
    concurrently, and a lone pass sends no cache marker (nothing would read it);
  * every finding that reaches a report is a quote-anchored pass finding: synthesis
    can only cite findings by id (domain/synthesis.py).
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import cast

from editta.domain.budget import check_budget, check_token_ceiling, project_remaining_cost_usd
from editta.domain.errors import EdittaError
from editta.domain.models import (
    CachePolicy,
    ManuscriptText,
    PassFindings,
    PassResult,
    SynthesisReport,
)
from editta.domain.ports import LLMClient
from editta.domain.quotes import anchor_findings
from editta.domain.synthesis import catalog_findings, resolve_synthesis
from editta.passes.base import AnalysisPass
from editta.passes.integrity import IntegrityPass
from editta.passes.language import LanguagePass
from editta.passes.metadata import MetadataPass
from editta.passes.structure import StructurePass
from editta.passes.synthesis import SynthesisPass

ANALYSIS_PASSES: tuple[AnalysisPass, ...] = (
    LanguagePass(),
    StructurePass(),
    MetadataPass(),
    IntegrityPass(),
)

PassCompletedHook = Callable[[PassResult], Awaitable[None]]


async def preflight(
    llm: LLMClient,
    manuscript: ManuscriptText,
    *,
    analysis_passes: Sequence[AnalysisPass],
    cost_ceiling_usd: float,
    include_synthesis: bool = True,
) -> int:
    """Refuse the job before spending anything if it is too long or over budget.

    Returns the prefix token count. `count_tokens` is free; the tokenizer, not a
    heuristic, decides — Greek tokenizes far denser than English (PLAN.md §4).
    """
    # shortcut: one count with the Opus tokenizer (Sonnet 5.5 shares it) stands in for
    # every model; count per model if a routed model's tokenizer counts higher.
    first = ANALYSIS_PASSES[0]
    prefix_tokens = await llm.count_tokens(first.build_prompt(manuscript), model=first.model)
    check_token_ceiling(prefix_tokens)
    projected = project_remaining_cost_usd(
        prefix_tokens=prefix_tokens,
        analysis_models=[p.model for p in analysis_passes],
        synthesis_model=SynthesisPass.model if include_synthesis else None,
    )
    check_budget(projected, cost_ceiling_usd)
    return prefix_tokens


async def run_pass(
    analysis_pass: AnalysisPass, manuscript: ManuscriptText, llm: LLMClient, *, cache: CachePolicy
) -> PassResult:
    prompt = analysis_pass.build_prompt(manuscript)
    result = await llm.analyze(
        prompt,
        analysis_pass.output_schema,
        cache=cache,
        effort=analysis_pass.effort,
        model=analysis_pass.model,
        fallbacks=analysis_pass.fallback_models,
    )
    # Concrete analysis passes always use PassFindings (base.py's Protocol only
    # guarantees BaseModel); synthesis has its own schema and its own function below.
    findings = cast(PassFindings, result.output).findings
    return PassResult(
        pass_name=analysis_pass.name,
        findings=anchor_findings(findings, manuscript.text),
        usage=result.usage,
        model=result.model,
    )


async def run_passes(
    passes: Sequence[AnalysisPass],
    manuscript: ManuscriptText,
    llm: LLMClient,
    *,
    on_pass_completed: PassCompletedHook | None = None,
) -> list[PassResult]:
    """Run `passes` in cache-prime-then-fan-out order per model group. Returns
    results in input order.

    If a fanned-out pass fails, its siblings still finish and are handed to the hook
    (i.e. checkpointed) before an error is raised — a retry then re-runs only the pass
    that failed, instead of paying again for the ones that succeeded.
    """
    if not passes:
        return []

    async def _run(p: AnalysisPass, cache: CachePolicy) -> PassResult:
        result = await run_pass(p, manuscript, llm, cache=cache)
        if on_pass_completed is not None:
            await on_pass_completed(result)
        return result

    async def _run_group(group: list[AnalysisPass]) -> list[PassResult]:
        first, *rest = group
        first_result = await _run(first, CachePolicy.WRITE if rest else CachePolicy.NONE)
        outcomes = await asyncio.gather(
            *(_run(p, CachePolicy.READ) for p in rest), return_exceptions=True
        )
        _raise_any(outcomes)
        return [first_result, *cast(list[PassResult], outcomes)]

    groups: dict[str, list[AnalysisPass]] = {}
    for p in passes:
        groups.setdefault(p.model, []).append(p)
    outcomes = await asyncio.gather(*map(_run_group, groups.values()), return_exceptions=True)
    _raise_any(outcomes)
    by_name = {r.pass_name: r for group in cast(list[list[PassResult]], outcomes) for r in group}
    return [by_name[p.name] for p in passes]


def _raise_any(outcomes: Sequence[object]) -> None:
    errors = [o for o in outcomes if isinstance(o, BaseException)]
    if errors:
        raise _most_decisive(errors)


def _most_decisive(errors: Sequence[BaseException]) -> BaseException:
    """Which sibling failure the job's fate should follow.

    Cancellation (shutdown) wins and propagates untouched. Otherwise a non-retryable
    error wins: retrying is pointless if any part is deterministically doomed, and the
    retryable siblings' work is already checkpointed anyway.
    """
    for error in errors:
        if isinstance(error, asyncio.CancelledError):
            return error
    for error in errors:
        if not (isinstance(error, EdittaError) and error.retryable):
            return error
    return errors[0]


async def run_synthesis(
    manuscript: ManuscriptText, pass_results: Sequence[PassResult], llm: LLMClient
) -> SynthesisReport:
    """Synthesis cites pass findings by id; the report is resolved from the anchored
    originals, so it can contain no finding the passes did not produce.

    CachePolicy.NONE: synthesis's schema differs from the passes', so it cannot read
    their cache (format changes invalidate it), and a cache_control marker here would
    only *write* a new entry at 1.25x for a prefix nothing else will reuse.
    """
    synthesis_pass = SynthesisPass()
    result = await llm.analyze(
        synthesis_pass.build_prompt(manuscript, list(pass_results)),
        synthesis_pass.output_schema,
        cache=CachePolicy.NONE,
        effort=synthesis_pass.effort,
        model=synthesis_pass.model,
        fallbacks=synthesis_pass.fallback_models,
    )
    return resolve_synthesis(result.output, catalog_findings(pass_results))
