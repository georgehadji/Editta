import pytest

from editta.domain.errors import BudgetExceeded, LLMError, LLMRequestRejected
from editta.domain.models import (
    CachePolicy,
    ManuscriptText,
    PassFindings,
    PassResult,
    PromptParts,
)
from editta.passes.base import SHARED_EFFORT, SYSTEM_PREAMBLE
from editta.services.analysis import ANALYSIS_PASSES, preflight, run_passes, run_synthesis
from tests.fakes import FakeLLMClient

TEXT = "Η γλώσσα του κειμένου παρουσιάζει επαναλήψεις στο δεύτερο κεφάλαιο του δοκιμίου."
MANUSCRIPT = ManuscriptText(source_filename="essay.docx", text=TEXT)


@pytest.mark.asyncio
async def test_the_four_passes_share_one_cacheable_prefix() -> None:
    llm = FakeLLMClient(manuscript_text=TEXT)

    await run_passes(ANALYSIS_PASSES, MANUSCRIPT, llm)

    assert len(llm.prompts) == 4
    assert {p.system for p in llm.prompts} == {SYSTEM_PREAMBLE}
    assert {p.manuscript_block for p in llm.prompts} == {TEXT}
    assert set(llm.efforts) == {SHARED_EFFORT}
    assert {p.output_schema for p in ANALYSIS_PASSES} == {PassFindings}


@pytest.mark.asyncio
async def test_synthesis_sends_no_cache_marker() -> None:
    """Its schema differs, so it cannot read the passes' cache; a marker would only
    write a fresh entry at 1.25x that nothing reuses."""
    llm = FakeLLMClient(manuscript_text=TEXT)
    results = await run_passes(ANALYSIS_PASSES, MANUSCRIPT, llm)

    await run_synthesis(MANUSCRIPT, results, llm)

    assert llm.calls[-1][1] is CachePolicy.NONE


@pytest.mark.asyncio
async def test_first_pass_primes_the_cache_alone_then_the_rest_read_it() -> None:
    llm = FakeLLMClient(manuscript_text=TEXT)

    await run_passes(ANALYSIS_PASSES, MANUSCRIPT, llm)

    assert [cache for _, cache in llm.calls] == [CachePolicy.WRITE] + [CachePolicy.READ] * 3


@pytest.mark.asyncio
async def test_completed_siblings_reach_the_hook_before_a_failure_propagates() -> None:
    def fail_structure(parts: PromptParts) -> BaseException | None:
        return (
            LLMError("overloaded") if parts.instructions.startswith("Analyze structure") else None
        )

    llm = FakeLLMClient(manuscript_text=TEXT, fail_when=fail_structure)
    checkpointed: list[str] = []

    async def hook(result: PassResult) -> None:
        checkpointed.append(result.pass_name)

    with pytest.raises(LLMError):
        await run_passes(ANALYSIS_PASSES, MANUSCRIPT, llm, on_pass_completed=hook)

    assert sorted(checkpointed) == ["integrity", "language", "metadata"]


@pytest.mark.asyncio
async def test_synthesis_can_only_cite_existing_findings() -> None:
    llm = FakeLLMClient(manuscript_text=TEXT, extra_synthesis_ids=["language-99", "injected"])
    results = await run_passes(ANALYSIS_PASSES, MANUSCRIPT, llm)
    originals = [f for r in results for f in r.findings]

    synthesis = await run_synthesis(MANUSCRIPT, results, llm)

    reported = [f for s in synthesis.sections for f in s.findings]
    assert len(reported) == len(originals)  # unknown ids dropped, nothing added
    assert all(f in originals for f in reported)


@pytest.mark.asyncio
async def test_a_non_retryable_sibling_failure_decides_the_job() -> None:
    def rule(parts: PromptParts) -> BaseException | None:
        if parts.instructions.startswith("Analyze structure"):
            return LLMError("overloaded")
        if parts.instructions.startswith("Analyze metadata"):
            return LLMRequestRejected("bad request")
        return None

    llm = FakeLLMClient(manuscript_text=TEXT, fail_when=rule)

    with pytest.raises(LLMRequestRejected):
        await run_passes(ANALYSIS_PASSES, MANUSCRIPT, llm)


@pytest.mark.asyncio
async def test_preflight_rejects_over_budget_without_calling_analyze() -> None:
    llm = FakeLLMClient(manuscript_text=TEXT)

    with pytest.raises(BudgetExceeded):
        await preflight(llm, MANUSCRIPT, analysis_passes_remaining=4, cost_ceiling_usd=0.001)

    assert llm.calls == []
    assert llm.token_counts == 1


@pytest.mark.asyncio
async def test_run_passes_with_nothing_to_do_makes_no_calls() -> None:
    llm = FakeLLMClient(manuscript_text=TEXT)
    assert await run_passes([], MANUSCRIPT, llm) == []
    assert llm.calls == []
