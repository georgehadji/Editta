import pytest

from editta.domain.errors import BudgetExceeded, LLMError
from editta.domain.models import (
    CachePolicy,
    Finding,
    ManuscriptText,
    PassResult,
    PromptParts,
    Severity,
)
from editta.passes.base import SHARED_EFFORT, SYSTEM_PREAMBLE
from editta.services.analysis import ANALYSIS_PASSES, preflight, run_passes, run_synthesis
from tests.fakes import FakeLLMClient

TEXT = "Η γλώσσα του κειμένου παρουσιάζει επαναλήψεις στο δεύτερο κεφάλαιο του δοκιμίου."
MANUSCRIPT = ManuscriptText(source_filename="essay.docx", text=TEXT)


@pytest.mark.asyncio
async def test_every_llm_call_for_a_manuscript_shares_the_cacheable_prefix() -> None:
    llm = FakeLLMClient(manuscript_text=TEXT)

    results = await run_passes(ANALYSIS_PASSES, MANUSCRIPT, llm)
    await run_synthesis(MANUSCRIPT, results, llm)

    assert len(llm.prompts) == 5
    assert {p.system for p in llm.prompts} == {SYSTEM_PREAMBLE}
    assert {p.manuscript_block for p in llm.prompts} == {TEXT}
    assert set(llm.efforts) == {SHARED_EFFORT}


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
async def test_synthesis_output_is_anchored_before_it_reaches_the_report() -> None:
    hallucinated = Finding(
        section="ch9",
        quote="μια φράση που δεν γράφτηκε ποτέ από τον συγγραφέα",
        issue="invented",
        severity=Severity.MAJOR,
        suggestion="n/a",
    )
    llm = FakeLLMClient(manuscript_text=TEXT, extra_synthesis_findings=[hallucinated])
    results = await run_passes(ANALYSIS_PASSES, MANUSCRIPT, llm)

    synthesis = await run_synthesis(MANUSCRIPT, results, llm)

    reported = [f for s in synthesis.sections for f in s.findings]
    assert hallucinated not in reported
    assert reported  # grounded findings echoed from the passes survive


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
