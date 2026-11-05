import io

import docx
import pytest

from editta.domain.errors import BudgetExceeded
from editta.domain.models import CachePolicy, PromptParts
from editta.services.pipeline import run_pipeline
from tests.fakes import FakeLLMClient

MANUSCRIPT_TEXT = "This is a Greek essay about language and structure and much more content here."


def _build_docx_bytes(text: str) -> bytes:
    document = docx.Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_run_pipeline_happy_path_produces_report() -> None:
    manuscript_bytes = _build_docx_bytes(MANUSCRIPT_TEXT)
    llm = FakeLLMClient(manuscript_text=MANUSCRIPT_TEXT)

    report = await run_pipeline(
        manuscript_bytes=manuscript_bytes, filename="essay.docx", llm=llm, cost_ceiling_usd=6.0
    )

    assert report.manuscript_filename == "essay.docx"
    assert len(report.pass_results) == 4
    assert report.synthesis.overview == "fake overview"
    assert not hasattr(report.synthesis, "recommendation")
    assert all(r.findings for r in report.pass_results)  # fake quotes are anchored, not dropped


@pytest.mark.asyncio
async def test_run_pipeline_primes_cache_before_fanning_out() -> None:
    manuscript_bytes = _build_docx_bytes(MANUSCRIPT_TEXT)
    llm = FakeLLMClient(manuscript_text=MANUSCRIPT_TEXT)

    await run_pipeline(
        manuscript_bytes=manuscript_bytes, filename="essay.docx", llm=llm, cost_ceiling_usd=6.0
    )

    caches = [cache for _, cache in llm.calls]
    assert caches == [CachePolicy.WRITE] + [CachePolicy.READ] * 3 + [CachePolicy.NONE]


@pytest.mark.asyncio
async def test_run_pipeline_aborts_when_manuscript_too_long() -> None:
    class HugeTokenLLM(FakeLLMClient):
        async def count_tokens(self, parts: PromptParts) -> int:
            return 800_000

    manuscript_bytes = _build_docx_bytes(MANUSCRIPT_TEXT)
    llm = HugeTokenLLM(manuscript_text=MANUSCRIPT_TEXT)

    with pytest.raises(BudgetExceeded):
        await run_pipeline(
            manuscript_bytes=manuscript_bytes, filename="essay.docx", llm=llm, cost_ceiling_usd=6.0
        )
