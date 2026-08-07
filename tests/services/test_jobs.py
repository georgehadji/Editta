import io
from uuid import uuid4

import docx
import pytest

from editta.domain.errors import BudgetExceeded
from editta.domain.states import JobState
from editta.services.jobs import JobRunner, _pass_checkpoint_key
from tests.fakes import FakeLLMClient
from tests.services.fakes import (
    FakeAnalysisJobRepo,
    FakeBlobStore,
    FakeLedgerRepo,
    FakeManuscriptRepo,
    FakePassResultRepo,
    FakeReportRepo,
    _PassResultRecord,
)

MANUSCRIPT_TEXT = "This is a Greek essay about language and structure and much more content here."


def _docx_bytes(text: str) -> bytes:
    document = docx.Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _make_runner(*, llm: FakeLLMClient) -> tuple[JobRunner, dict[str, object]]:
    jobs = FakeAnalysisJobRepo()
    manuscripts = FakeManuscriptRepo()
    pass_results = FakePassResultRepo()
    reports = FakeReportRepo()
    ledger = FakeLedgerRepo()
    blobs = FakeBlobStore()
    runner = JobRunner(
        jobs=jobs,  # type: ignore[arg-type]
        manuscripts=manuscripts,  # type: ignore[arg-type]
        pass_results=pass_results,  # type: ignore[arg-type]
        reports=reports,  # type: ignore[arg-type]
        ledger=ledger,  # type: ignore[arg-type]
        blobs=blobs,  # type: ignore[arg-type]
        llm=llm,
    )
    parts = {
        "jobs": jobs,
        "manuscripts": manuscripts,
        "pass_results": pass_results,
        "reports": reports,
        "ledger": ledger,
        "blobs": blobs,
    }
    return runner, parts


@pytest.mark.asyncio
async def test_run_happy_path_completes_and_writes_report() -> None:
    tenant_id, job_id, manuscript_id = uuid4(), uuid4(), uuid4()
    llm = FakeLLMClient(manuscript_text=MANUSCRIPT_TEXT)
    runner, parts = _make_runner(llm=llm)
    parts["manuscripts"].seed(
        tenant_id, manuscript_id, blob_key="raw/essay.docx", title="essay.docx"
    )
    parts["blobs"].seed("raw/essay.docx", _docx_bytes(MANUSCRIPT_TEXT))

    await runner.run(tenant_id=tenant_id, job_id=job_id, manuscript_id=manuscript_id)

    states = [s for s, _ in parts["jobs"].state_history]
    assert states == [
        JobState.PARSING,
        JobState.ANALYZING,
        JobState.SYNTHESIZING,
        JobState.EXPORTING,
        JobState.COMPLETED,
    ]
    assert len(await parts["pass_results"].list_for_job(job_id)) == 4
    assert len(parts["reports"].created) == 1
    reserve_entries = [e for e in parts["ledger"].entries if e["reason"] == "job"]
    assert len(reserve_entries) == 1 and reserve_entries[0]["delta"] == -1


@pytest.mark.asyncio
async def test_run_resumes_from_existing_checkpoints_without_recalling_llm() -> None:
    tenant_id, job_id, manuscript_id = uuid4(), uuid4(), uuid4()
    llm = FakeLLMClient(manuscript_text=MANUSCRIPT_TEXT)
    runner, parts = _make_runner(llm=llm)
    parts["manuscripts"].seed(
        tenant_id, manuscript_id, blob_key="raw/essay.docx", title="essay.docx"
    )
    parts["blobs"].seed("raw/essay.docx", _docx_bytes(MANUSCRIPT_TEXT))

    # Pre-checkpoint 2 of the 4 passes, as if a prior attempt got that far before crashing.
    for pass_name in ("language", "structure"):
        key = _pass_checkpoint_key(job_id, pass_name)
        parts["blobs"].seed(key, b"[]")
        usage = {"input_tokens": 1, "output_tokens": 1}
        parts["pass_results"].seed(_PassResultRecord(job_id, pass_name, key, "fake-model", usage))

    await runner.run(tenant_id=tenant_id, job_id=job_id, manuscript_id=manuscript_id)

    # Only the 2 missing passes + synthesis should have hit the LLM — not all 4 + synthesis.
    assert len(llm.calls) == 3


@pytest.mark.asyncio
async def test_run_refunds_credit_and_marks_failed_on_budget_exceeded() -> None:
    tenant_id, job_id, manuscript_id = uuid4(), uuid4(), uuid4()

    class HugeTokenLLM(FakeLLMClient):
        async def count_tokens(self, parts: object) -> int:  # type: ignore[override]
            return 800_000

    llm = HugeTokenLLM(manuscript_text=MANUSCRIPT_TEXT)
    runner, parts = _make_runner(llm=llm)
    parts["manuscripts"].seed(
        tenant_id, manuscript_id, blob_key="raw/essay.docx", title="essay.docx"
    )
    parts["blobs"].seed("raw/essay.docx", _docx_bytes(MANUSCRIPT_TEXT))

    with pytest.raises(BudgetExceeded):
        await runner.run(tenant_id=tenant_id, job_id=job_id, manuscript_id=manuscript_id)

    assert parts["jobs"].state_history[-1][0] == JobState.FAILED
    refund_entries = [e for e in parts["ledger"].entries if e["reason"] == "refund"]
    assert len(refund_entries) == 1 and refund_entries[0]["delta"] == 1
